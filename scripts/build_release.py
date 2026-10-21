#!/usr/bin/env python3
"""Build a portable, task-free zip from an explicit distribution allowlist."""
from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

from workflow import VERSION


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"promo-shopping-assistant-{VERSION}.zip"
    paths = [root / name for name in ("SKILL.md", "README.md", "PLAN.md")]
    excluded_dirs = {"__pycache__", ".venv", "browser-profile", "evidence"}
    excluded_files = {"browser-cart.json", "cart-snapshot.json"}
    for folder in ("scripts", "references", "fixtures", "agents", "docs", "tests", "optional"):
        paths.extend(p for p in (root / folder).rglob("*") if p.is_file()
                     and not (set(p.parts) & excluded_dirs)
                     and p.suffix != ".pyc" and p.name not in excluded_files)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(paths):
            stream.write(path, Path("promo-shopping-assistant") / path.relative_to(root))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    print(archive)
    print(f"sha256: {digest}")


if __name__ == "__main__":
    main()
