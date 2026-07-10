#!/usr/bin/env python3
"""Render systemd source templates for validation without installing them."""

from __future__ import annotations

import argparse
import grp
import os
from pathlib import Path
import pwd
import re


TOKENS = {
    "@APP_USER@": "app_user",
    "@APP_GROUP@": "app_group",
    "@APP_HOME@": "app_home",
    "@PROJECT_DIR@": "project_dir",
    "@DISPLAY_PORT@": "display_port",
}


def render(source: Path, destination: Path, values: dict[str, str]) -> None:
    text = source.read_text(encoding="utf-8")
    for token, key in TOKENS.items():
        text = text.replace(token, values[key])
    unresolved = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", text)))
    if unresolved:
        raise ValueError(f"{source}: unresolved template tokens: {', '.join(unresolved)}")
    destination.write_text(text, encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    user = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--app-user", default=user.pw_name)
    parser.add_argument("--app-group", default=group.gr_name)
    parser.add_argument("--app-home", default=user.pw_dir)
    parser.add_argument("--project-dir", default=str(root))
    parser.add_argument("--display-port", default="5000")
    args = parser.parse_args()

    if not args.display_port.isdigit() or not 1 <= int(args.display_port) <= 65535:
        raise SystemExit("display port must be between 1 and 65535")
    args.output.mkdir(parents=True, exist_ok=True)
    values = {
        "app_user": args.app_user,
        "app_group": args.app_group,
        "app_home": args.app_home,
        "project_dir": args.project_dir,
        "display_port": args.display_port,
    }
    for pattern in ("*.service", "*.path"):
        for source in sorted((root / "services").glob(pattern)):
            render(source, args.output / source.name, values)
    for source in sorted((root / "tmpfiles.d").glob("*.conf")):
        render(source, args.output / source.name, values)


if __name__ == "__main__":
    main()
