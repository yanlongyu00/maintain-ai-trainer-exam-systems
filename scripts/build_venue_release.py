#!/usr/bin/env python3
"""Build deterministic Netlify and Miaoda artifacts for the venue system."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
REQUIRED_WHEELS = (
    "et_xmlfile-2.0.0-py3-none-any.whl",
    "openpyxl-3.1.5-py2.py3-none-any.whl",
)
REQUIRED_MARKERS = (
    "const ACCESS_CODE",
    "VENUE_BUILD_VERSION",
    "${idx + 1} 标准输出",
    "考试中请以右侧标准答案为参考",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and verify venue release artifacts without overwriting old packages."
    )
    parser.add_argument("--source", required=True, type=Path, help="Tested venue.html")
    parser.add_argument("--output-dir", required=True, type=Path, help="New or empty output directory")
    parser.add_argument("--vendor-dir", type=Path, help="Defaults to <source-dir>/vendor")
    parser.add_argument("--redirects", type=Path, help="Defaults to <source-dir>/_redirects")
    return parser.parse_args()


def fail(message: str) -> None:
    raise ValueError(message)


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    vendor_dir = (args.vendor_dir or source.parent / "vendor").expanduser().resolve()
    redirects = (args.redirects or source.parent / "_redirects").expanduser().resolve()

    if not source.is_file():
        fail(f"source not found: {source}")
    if source.suffix.lower() != ".html":
        fail("source must be an HTML file")
    if output_dir.exists() and any(output_dir.iterdir()):
        fail(f"output directory is not empty: {output_dir}")
    if not redirects.is_file():
        fail(f"redirects file not found: {redirects}")

    source_bytes = source.read_bytes()
    if len(source_bytes) > MAX_HTML_BYTES:
        fail(f"HTML exceeds 10MB: {len(source_bytes)} bytes")
    source_text = source_bytes.decode("utf-8")
    missing_markers = [marker for marker in REQUIRED_MARKERS if marker not in source_text]
    if missing_markers:
        fail("source is missing release markers: " + ", ".join(missing_markers))

    version_match = re.search(r"VENUE_BUILD_VERSION\s*=\s*['\"]([^'\"]+)['\"]", source_text)
    if not version_match:
        fail("VENUE_BUILD_VERSION value not found")
    version = version_match.group(1)
    version_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", version).strip("-")

    wheel_data: dict[str, bytes] = {}
    for name in REQUIRED_WHEELS:
        wheel_path = vendor_dir / name
        if not wheel_path.is_file():
            fail(f"required wheel not found: {wheel_path}")
        wheel_data[name] = wheel_path.read_bytes()

    redirects_bytes = redirects.read_bytes()
    archive_files: dict[str, bytes] = {
        "index.html": source_bytes,
        "venue.html": source_bytes,
        "_redirects": redirects_bytes,
    }
    for name, data in wheel_data.items():
        archive_files[f"vendor/{name}"] = data

    total_bytes = sum(len(data) for data in archive_files.values())
    if total_bytes > MAX_TOTAL_BYTES:
        fail(f"uncompressed package exceeds 200MB: {total_bytes} bytes")

    output_dir.mkdir(parents=True, exist_ok=True)
    netlify_dir = output_dir / "netlify"
    miaoda_dir = output_dir / "miaoda"
    for name, data in archive_files.items():
        write_bytes(netlify_dir / name, data)
    write_bytes(miaoda_dir / "index.html", source_bytes)

    zip_path = output_dir / f"venue-netlify-{version_slug}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(archive_files):
            archive.writestr(zip_info(name), archive_files[name])

    if zip_path.stat().st_size > MAX_ZIP_BYTES:
        fail(f"ZIP exceeds 20MB: {zip_path.stat().st_size} bytes")

    with zipfile.ZipFile(zip_path) as archive:
        actual_names = sorted(name for name in archive.namelist() if not name.endswith("/"))
    expected_names = sorted(archive_files)
    if actual_names != expected_names:
        fail(f"ZIP root mismatch: expected {expected_names}, got {actual_names}")

    source_hash = sha256_bytes(source_bytes)
    copied_hashes = {
        "netlify/index.html": sha256_bytes((netlify_dir / "index.html").read_bytes()),
        "netlify/venue.html": sha256_bytes((netlify_dir / "venue.html").read_bytes()),
        "miaoda/index.html": sha256_bytes((miaoda_dir / "index.html").read_bytes()),
    }
    if any(value != source_hash for value in copied_hashes.values()):
        fail("copied HTML hash does not match tested source")

    manifest = {
        "system": "venue",
        "version": version,
        "source": str(source),
        "source_sha256": source_hash,
        "zip": zip_path.name,
        "zip_sha256": sha256_bytes(zip_path.read_bytes()),
        "zip_bytes": zip_path.stat().st_size,
        "uncompressed_bytes": total_bytes,
        "archive_files": {
            name: {"bytes": len(data), "sha256": sha256_bytes(data)}
            for name, data in sorted(archive_files.items())
        },
        "copied_html_sha256": copied_hashes,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
