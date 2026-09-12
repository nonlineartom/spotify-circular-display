#!/usr/bin/env python3
"""Validate and render the deliberately narrow LAN HTTPS nginx site."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "deploy/nginx/spotify-display-lan-https.conf.template"
EXPECTED_KEYS = {
    "public_host",
    "lan_listen_address",
    "lan_allow_cidr",
    "tls_certificate_path",
    "tls_private_key_path",
    "flask_port",
}
RFC1918 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SAFE_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9._/+:-]+$")
TOKEN = re.compile(r"@[A-Z][A-Z0-9_]*@")


class SettingsError(ValueError):
    pass


def _required_string(settings: dict[str, object], key: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{key} must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise SettingsError(f"{key} contains whitespace or control characters")
    return value


def _validate_host(value: str) -> str:
    host = value.lower()
    if len(host) > 253 or "." not in host or host.endswith("."):
        raise SettingsError("public_host must be a complete DNS hostname without a trailing dot")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SettingsError("public_host must be a DNS name, not an IP address")
    if host.endswith(
        (
            ".local",
            ".localhost",
            ".internal",
            ".invalid",
            ".test",
            ".example",
            ".home.arpa",
            ".onion",
        )
    ):
        raise SettingsError("public_host must be eligible for a publicly trusted certificate")
    if any(not DNS_LABEL.fullmatch(label) for label in host.split(".")):
        raise SettingsError("public_host contains an invalid DNS label")
    return host


def _validate_path(value: str, key: str, require_files: bool) -> str:
    if not SAFE_ABSOLUTE_PATH.fullmatch(value):
        raise SettingsError(f"{key} must be a simple absolute Linux path")
    if require_files and not Path(value).is_file():
        raise SettingsError(f"{key} is not an existing file: {value}")
    return value


def load_settings(path: Path, require_files: bool = False) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SettingsError(f"cannot read settings: {error}") from error
    if not isinstance(raw, dict):
        raise SettingsError("settings must be a JSON object")
    unknown = set(raw) - EXPECTED_KEYS
    missing = EXPECTED_KEYS - set(raw)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise SettingsError("invalid settings keys: " + "; ".join(details))

    host = _validate_host(_required_string(raw, "public_host"))
    address_text = _required_string(raw, "lan_listen_address")
    cidr_text = _required_string(raw, "lan_allow_cidr")
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError as error:
        raise SettingsError("lan_listen_address must be an IPv4 address") from error
    if not isinstance(address, ipaddress.IPv4Address) or not any(address in block for block in RFC1918):
        raise SettingsError("lan_listen_address must be a concrete RFC1918 IPv4 address")
    try:
        network = ipaddress.ip_network(cidr_text, strict=True)
    except ValueError as error:
        raise SettingsError("lan_allow_cidr must be a canonical IPv4 network") from error
    if not isinstance(network, ipaddress.IPv4Network) or not any(
        network.subnet_of(block) for block in RFC1918
    ):
        raise SettingsError("lan_allow_cidr must be wholly inside one RFC1918 range")
    if address not in network:
        raise SettingsError("lan_allow_cidr must contain lan_listen_address")

    port = raw.get("flask_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SettingsError("flask_port must be an integer from 1 to 65535")

    certificate = _validate_path(
        _required_string(raw, "tls_certificate_path"),
        "tls_certificate_path",
        require_files,
    )
    private_key = _validate_path(
        _required_string(raw, "tls_private_key_path"),
        "tls_private_key_path",
        require_files,
    )
    return {
        "PUBLIC_HOST": host,
        "LAN_LISTEN_ADDRESS": str(address),
        "LAN_ALLOW_CIDR": str(network),
        "TLS_CERTIFICATE_PATH": certificate,
        "TLS_PRIVATE_KEY_PATH": private_key,
        "FLASK_PORT": str(port),
    }


def render(template_path: Path, settings: dict[str, str]) -> str:
    try:
        rendered = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SettingsError(f"cannot read template: {error}") from error
    expected_tokens = {f"@{key}@" for key in settings}
    actual_tokens = set(TOKEN.findall(rendered))
    if actual_tokens != expected_tokens:
        raise SettingsError(
            "template token mismatch: expected "
            + ", ".join(sorted(expected_tokens))
            + "; found "
            + ", ".join(sorted(actual_tokens))
        )
    for key, value in settings.items():
        rendered = rendered.replace(f"@{key}@", value)
    if TOKEN.search(rendered):
        raise SettingsError("rendered configuration contains unresolved tokens")
    return rendered


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path, help="filled LAN HTTPS JSON settings")
    parser.add_argument("output", type=Path, help="rendered nginx site path")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--require-files",
        action="store_true",
        help="also require the certificate and private-key paths to exist",
    )
    args = parser.parse_args()
    try:
        settings = load_settings(args.settings, require_files=args.require_files)
        atomic_write(args.output, render(args.template, settings))
    except SettingsError as error:
        parser.error(str(error))
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
