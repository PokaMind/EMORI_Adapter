#!/usr/bin/env python3
"""Make one add-on importable and discoverable from a site-packages directory.

    addon_dist.py <addon-directory> <site-packages>

The add-on's code stays where the deploy put it. A path file points at it, so whatever the
add-on reaches relative to its package still resolves and a new copy of its code needs no
reinstall. A dist-info directory carries its entry points, which is how the core finds it.

Both are written from the add-on's pyproject.toml. No build backend runs on the robot, so this
works the same with whichever packaging tools an image happens to carry.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


def _distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _source_root(addon: Path, pyproject: dict) -> Path:
    found = (pyproject.get("tool", {}).get("setuptools", {})
             .get("packages", {}).get("find", {}).get("where"))
    if found:
        return addon / found[0]
    return addon / "src" if (addon / "src").is_dir() else addon


def _entry_points(project: dict) -> str:
    lines: list[str] = []
    for group, entries in sorted(project.get("entry-points", {}).items()):
        lines.append(f"[{group}]")
        lines.extend(f"{name} = {target}" for name, target in sorted(entries.items()))
        lines.append("")
    return "\n".join(lines)


def write(addon: Path, site: Path) -> Path:
    """Write the path file and the dist-info. Returns the path file."""

    addon = addon.resolve()
    pyproject = tomllib.loads((addon / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    name, version = project["name"], project["version"]
    distribution = _distribution(name)

    info = site / f"{distribution}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(_entry_points(project), encoding="utf-8")
    (info / "INSTALLER").write_text("hub-addons\n", encoding="utf-8")

    path_file = site / f"{distribution}.pth"
    path_file.write_text(f"{_source_root(addon, pyproject).as_posix()}\n", encoding="utf-8")
    return path_file


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    write(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
