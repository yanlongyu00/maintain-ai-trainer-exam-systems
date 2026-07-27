#!/usr/bin/env python3
"""Read-only baseline audit for the three exam systems."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MATERIALS = Path.cwd()
DEFAULT_LEGACY = Path.cwd()

CANDIDATES = {
    "theory": [
        ("materials-netlify", "netlify-main-deploy/index.html"),
        ("materials-miaoda", "theory-backup-site/index.html"),
        ("legacy-source", "exam.html"),
        ("legacy-share", "share/index.html"),
    ],
    "hands-on": [
        ("materials-netlify", "netlify-main-deploy/hands-on.html"),
        ("materials-miaoda", "domestic-backup-site/index.html"),
        ("legacy-source", "hands-on.html"),
        ("legacy-share", "share/hands-on.html"),
    ],
    "venue": [
        ("materials-main-candidate", "netlify-main-deploy/venue.html"),
        ("materials-venue-candidate", "venue-netlify-deploy/index.html"),
        ("legacy-source", "venue.html"),
        ("legacy-share", "share/venue.html"),
    ],
}

MARKERS = {
    "theory": ["wrongBook", "practiceProgress", "APP_VERSION"],
    "hands-on": ["VALID_CODE_HASHES", "localStorage"],
    "venue": ["PYODIDE_VERSION", "venue_progress_", "localStorage"],
}

THEORY_PROVIDER_VARIANT_PATTERNS = [
    re.compile(
        r'<script\s+async\s+src=["\']https://busuanzi\.ibruce\.info/busuanzi/2\.3/'
        r'busuanzi\.pure\.mini\.js["\']></script>\s*',
        re.I,
    ),
    re.compile(
        r'<div\s+id=["\']busuanzi_container_site_uv["\'][^>]*>.*?</div>\s*',
        re.I | re.S,
    ),
]


class ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_inline_script = False
        self.current: List[str] = []
        self.scripts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "script":
            return
        values = dict(attrs)
        self.in_inline_script = not values.get("src") and values.get("type", "text/javascript") in {
            "text/javascript",
            "application/javascript",
            "module",
        }
        self.current = []

    def handle_data(self, data: str) -> None:
        if self.in_inline_script:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.in_inline_script:
            script = "".join(self.current).strip()
            if script:
                self.scripts.append(script)
            self.in_inline_script = False
            self.current = []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_business_html(text: str, system: str) -> str:
    normalized = text.replace("\r\n", "\n")
    if system == "theory":
        for pattern in THEORY_PROVIDER_VARIANT_PATTERNS:
            normalized = pattern.sub("", normalized)
    return normalized


def syntax_check(text: str) -> Dict[str, Any]:
    node = shutil.which("node")
    if not node:
        return {"status": "skipped", "reason": "node not found"}
    parser = ScriptCollector()
    try:
        parser.feed(text)
    except Exception as exc:  # HTMLParser is lenient, but report unexpected failures.
        return {"status": "failed", "reason": f"HTML parse: {exc}"}
    failures: List[str] = []
    for index, script in enumerate(parser.scripts, start=1):
        suffix = ".mjs" if re.search(r"\b(import|export)\b", script) else ".js"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, encoding="utf-8") as tmp:
            tmp.write(script)
            tmp.flush()
            result = subprocess.run([node, "--check", tmp.name], capture_output=True, text=True)
        if result.returncode:
            failures.append(f"script {index}: {(result.stderr or result.stdout).strip()}")
    if failures:
        return {"status": "failed", "errors": failures[:5], "script_count": len(parser.scripts)}
    return {"status": "passed", "script_count": len(parser.scripts)}


def inspect(path: Path, system: str, label: str) -> Dict[str, Any]:
    item: Dict[str, Any] = {"label": label, "path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return item
    text = path.read_text(encoding="utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    item.update(
        {
            "bytes": path.stat().st_size,
            "miaoda_single_html_limit_ok": path.stat().st_size <= 10 * 1024 * 1024,
            "sha256": sha256(path),
            "business_sha256": text_sha256(normalize_business_html(text, system)),
            "title": re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else None,
            "markers": {marker: marker in text for marker in MARKERS[system]},
            "javascript_syntax": syntax_check(text),
        }
    )
    versions = re.findall(r"(?:APP_VERSION|PYODIDE_VERSION)\s*=\s*['\"]([^'\"]+)", text)
    item["version_markers"] = sorted(set(versions))
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", choices=["theory", "hands-on", "venue", "all"], default="all")
    parser.add_argument("--materials-root", type=Path, default=DEFAULT_MATERIALS)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    selected = list(CANDIDATES) if args.system == "all" else [args.system]
    report: Dict[str, Any] = {"read_only": True, "systems": {}}
    for system in selected:
        rows = []
        for label, relative in CANDIDATES[system]:
            root = args.materials_root if label.startswith("materials-") else args.legacy_root
            rows.append(inspect(root / relative, system, label))
        existing_hashes = {row.get("sha256") for row in rows if row.get("sha256")}
        existing_business_hashes = {
            row.get("business_sha256") for row in rows if row.get("business_sha256")
        }
        exact_equal = len(existing_hashes) <= 1
        business_equal = len(existing_business_hashes) <= 1
        report["systems"][system] = {
            "candidates": rows,
            "all_existing_hashes_equal": exact_equal,
            "all_existing_business_hashes_equal": business_equal,
            "known_provider_variants_only": not exact_equal and business_equal,
            "warning": "Do not choose an authority by mtime; resolve divergent copies before editing."
            if not business_equal
            else None,
            "note": "Exact files differ only by known hosting-provider adaptations."
            if not exact_equal and business_equal
            else None,
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for system, details in report["systems"].items():
            print(
                f"[{system}] exact_equal={details['all_existing_hashes_equal']} "
                f"business_equal={details['all_existing_business_hashes_equal']}"
            )
            for row in details["candidates"]:
                if not row["exists"]:
                    print(f"  MISSING {row['label']}: {row['path']}")
                    continue
                syntax = row["javascript_syntax"]["status"]
                print(
                    f"  {row['label']}: {row['bytes']} bytes sha256={row['sha256'][:12]} "
                    f"business={row['business_sha256'][:12]} js={syntax} title={row['title']!r}"
                )
            if details["note"]:
                print(f"  NOTE: {details['note']}")
            if details["warning"]:
                print(f"  WARNING: {details['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
