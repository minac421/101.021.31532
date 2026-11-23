"""Check that every relative Markdown link (and #anchor) in the repository resolves.

Anchors are computed like GitHub's slugger: lower-case, punctuation removed,
spaces turned into hyphens. Linked headings must not contain Arabic diacritics,
whose handling differs between renderers.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path


def slug(heading: str) -> str:
    out = []
    for ch in heading.strip().lower():
        category = unicodedata.category(ch)
        if ch == " ":
            out.append("-")
        elif ch in "-_" or category[0] in ("L", "N") or category in ("Mn", "Mc"):
            out.append(ch)
    return "".join(out)


def headings(path: Path) -> set[str]:
    found, in_code = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and (m := re.match(r"^#{1,6}\s+(.*)$", line)):
            found.add(slug(m.group(1)))
    return found


def main(root: Path) -> int:
    problems = checked = 0
    for path in sorted(root.rglob("*.md")):
        if any(part in {".git", ".venv", "node_modules"} for part in path.parts):
            continue
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            if not dest.exists():
                print(f"missing file: {path.relative_to(root)} -> {target}")
                problems += 1
            elif anchor and anchor not in headings(dest):
                print(f"missing anchor: {path.relative_to(root)} -> {target}")
                problems += 1
            elif anchor and any(unicodedata.category(c) in ("Mn", "Mc") for c in anchor):
                print(f"anchor with diacritics (renderer-dependent): {path.relative_to(root)} -> {target}")
                problems += 1
    print(f"checked {checked} relative links, {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).resolve()))
