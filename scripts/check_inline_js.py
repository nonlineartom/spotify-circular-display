#!/usr/bin/env python3
"""Syntax-check inline JavaScript in the kiosk templates with Node.js."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._collecting = False
        self._external = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        self._collecting = True
        self._external = bool(attributes.get("src"))
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting and not self._external:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._collecting:
            return
        if not self._external:
            self.scripts.append("".join(self._parts))
        self._collecting = False
        self._external = False
        self._parts = []


def neutralise_jinja(source: str) -> str:
    """Replace simple server template expressions with valid JS literals."""
    source = re.sub(r"\{\{.*?\}\}", "null", source, flags=re.DOTALL)
    return re.sub(r"\{%.*?%\}", "", source, flags=re.DOTALL)


def check_template(path: Path, node: str) -> None:
    parser = ScriptCollector()
    parser.feed(path.read_text(encoding="utf-8"))
    for number, script in enumerate(parser.scripts, start=1):
        if not script.strip():
            continue
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=f"-{path.stem}-{number}.js", encoding="utf-8"
        ) as temporary:
            temporary.write(neutralise_jinja(script))
            temporary.flush()
            subprocess.run([node, "--check", temporary.name], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("templates", nargs="+", type=Path)
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is required to syntax-check inline JavaScript")
    for template in args.templates:
        check_template(template, node)


if __name__ == "__main__":
    main()
