#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Install a development symlink without replacing existing user content."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--plugins-dir", type=Path, default=Path.home() / ".local/share/QGIS/QGIS3/profiles/default/python/plugins")
args = parser.parse_args()
source = Path(__file__).resolve().parents[1] / "acalc"
target = args.plugins_dir.expanduser() / "acalc"
target.parent.mkdir(parents=True, exist_ok=True)
if target.is_symlink() and target.resolve() == source:
    print(f"Already linked: {target} → {source}")
elif target.exists() or target.is_symlink():
    parser.error(f"Refusing to replace existing path: {target}")
else:
    target.symlink_to(source, target_is_directory=True)
    print(f"Linked: {target} → {source}")
