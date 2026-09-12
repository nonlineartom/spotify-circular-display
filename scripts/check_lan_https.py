#!/usr/bin/env python3
"""Static contract checks for the LAN-only nginx renderer."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from render_lan_https import SettingsError, TOKEN, load_settings, render


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/nginx/spotify-display-lan-https.conf.template"
VALID = {
    "public_host": "player.example.com",
    "lan_listen_address": "192.168.50.20",
    "lan_allow_cidr": "192.168.50.0/24",
    "tls_certificate_path": "/etc/spotify-display/tls/fullchain.pem",
    "tls_private_key_path": "/etc/spotify-display/tls/privkey.pem",
    "flask_port": 5000,
}


def expect_invalid(path: Path, **updates: object) -> None:
    settings = {**VALID, **updates}
    path.write_text(json.dumps(settings), encoding="utf-8")
    try:
        load_settings(path)
    except SettingsError:
        return
    raise AssertionError(f"invalid LAN HTTPS settings were accepted: {updates}")


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps(VALID), encoding="utf-8")
        settings = load_settings(settings_path)
        output = render(TEMPLATE, settings)

        required = (
            "listen 192.168.50.20:443 ssl default_server;",
            "allow 192.168.50.0/24;",
            "proxy_pass http://127.0.0.1:5000;",
            "proxy_set_header Host $host;",
            "proxy_set_header X-Forwarded-Proto https;",
            "proxy_set_header Authorization \"\";",
            "location = /api { return 404; }",
            "location ^~ /api/ { return 404; }",
            "location / { return 404; }",
        )
        for fragment in required:
            assert fragment in output, f"rendered nginx contract missing: {fragment}"
        assert "0.0.0.0" not in output
        assert "listen 80" not in output
        assert TOKEN.search(output) is None

        for suffix in (
            "local",
            "localhost",
            "internal",
            "invalid",
            "test",
            "example",
            "home.arpa",
            "onion",
        ):
            expect_invalid(settings_path, public_host=f"player.{suffix}")
        expect_invalid(settings_path, public_host="192.168.50.20")
        expect_invalid(settings_path, lan_listen_address="203.0.113.20")
        expect_invalid(settings_path, lan_listen_address="100.64.0.20")
        expect_invalid(settings_path, lan_allow_cidr="192.168.51.0/24")
        expect_invalid(settings_path, lan_allow_cidr="192.168.50.20/24")
        expect_invalid(settings_path, tls_private_key_path="relative/key.pem")

        extra = {**VALID, "wan_address": "203.0.113.20"}
        settings_path.write_text(json.dumps(extra), encoding="utf-8")
        try:
            load_settings(settings_path)
        except SettingsError:
            pass
        else:
            raise AssertionError("unknown settings key was accepted")

    print("LAN HTTPS renderer checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
