"""Type-safe helpers for Spotify library profiles stored in ``config.json``.

The Connect receiver and Spotify Web API use separate credentials.  This
module keeps the Web API side deliberately small and serialisable: profiles
are keyed by Spotify account id, while receiver usernames are aliases that
select a profile.  Access-token caching remains process-local in ``server``;
only refresh grants and bounded profile metadata are persisted here.
"""

from __future__ import annotations

import math
import unicodedata


STORE_VERSION = 1
MAX_ID_LENGTH = 256
MAX_NAME_LENGTH = 256
MAX_ALIASES = 16
MAX_PROFILES = 32
MAX_SCOPES = 32
MAX_REFRESH_TOKEN_LENGTH = 8192


class AliasCollisionError(ValueError):
    """Raised when one opaque receiver alias would select two accounts."""


class ProfileLimitError(ValueError):
    """Raised when a new profile would exceed the bounded durable store."""


def normalize_identifier(value):
    """Return a stable, bounded identifier or ``None`` for unsafe input."""
    if not isinstance(value, str):
        return None
    # Spotify identifiers are opaque and case-sensitive.  Never case-fold or
    # Unicode-normalise them: visually similar strings may be distinct users.
    if (
        not value
        or value != value.strip()
        or len(value) > MAX_ID_LENGTH
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    return value


def normalize_alias(value):
    return normalize_identifier(value)


def _finite_timestamp(value, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _normalise_scopes(value):
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list):
        return []
    scopes = []
    for scope in value:
        if (
            isinstance(scope, str)
            and scope
            and len(scope) <= 128
            and scope not in scopes
            and all(char.isalnum() or char in "-_:" for char in scope)
        ):
            scopes.append(scope)
        if len(scopes) >= MAX_SCOPES:
            break
    return scopes


def normalize_profile(value, account_id=None):
    if not isinstance(value, dict):
        return None
    account_id = normalize_identifier(account_id or value.get("account_id"))
    refresh_token = value.get("refresh_token")
    if (
        not account_id
        or not isinstance(refresh_token, str)
        or not refresh_token
        or len(refresh_token) > MAX_REFRESH_TOKEN_LENGTH
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in refresh_token)
    ):
        return None

    display_name = value.get("display_name")
    if not isinstance(display_name, str):
        display_name = ""
    display_name = unicodedata.normalize("NFKC", display_name).strip()[:MAX_NAME_LENGTH]

    kind = value.get("kind")
    if kind not in ("guest", "owner"):
        kind = "guest"
    connected_at = _finite_timestamp(value.get("connected_at")) or 0.0
    expires_at = _finite_timestamp(value.get("expires_at"), allow_none=True)
    if kind == "guest" and expires_at is None:
        return None

    aliases = []
    raw_aliases = value.get("receiver_aliases")
    if isinstance(raw_aliases, list):
        for alias in raw_aliases:
            alias = normalize_alias(alias)
            if alias and alias not in aliases:
                aliases.append(alias)
            if len(aliases) >= MAX_ALIASES:
                break

    profile = {
        "account_id": account_id,
        "display_name": display_name,
        "refresh_token": refresh_token,
        "kind": kind,
        "connected_at": connected_at,
        "expires_at": expires_at,
        "scopes": _normalise_scopes(value.get("scopes")),
        "receiver_aliases": aliases,
    }
    return profile


def normalize_store(value):
    raw = value if isinstance(value, dict) else {}
    profiles = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for raw_id, raw_profile in raw_profiles.items():
            profile = normalize_profile(raw_profile, raw_id)
            if profile:
                profiles[profile["account_id"]] = profile
            if len(profiles) >= MAX_PROFILES:
                break

    aliases = {}
    collisions = set()
    # Rebuild the index from each profile instead of trusting a second mutable
    # representation from disk. Ambiguity fails closed: a colliding alias maps
    # to no profile until an owner repairs or removes one of the grants.
    for account_id, profile in profiles.items():
        for alias in profile["receiver_aliases"]:
            previous = aliases.get(alias)
            if alias in collisions:
                continue
            if previous and previous != account_id:
                aliases.pop(alias, None)
                collisions.add(alias)
            else:
                aliases[alias] = account_id

    return {"version": STORE_VERSION, "profiles": profiles, "aliases": aliases}


def profile_for_alias(store, alias):
    store = normalize_store(store)
    alias = normalize_alias(alias)
    account_id = store["aliases"].get(alias) if alias else None
    profile = store["profiles"].get(account_id) if account_id else None
    return dict(profile) if profile else None


def upsert_profile(store, profile, receiver_alias=None):
    store = normalize_store(store)
    profile = normalize_profile(profile)
    if not profile:
        raise ValueError("invalid Spotify profile")

    account_id = profile["account_id"]
    if account_id not in store["profiles"] and len(store["profiles"]) >= MAX_PROFILES:
        raise ProfileLimitError("Spotify profile limit reached")
    alias = normalize_alias(receiver_alias)
    existing = store["profiles"].get(account_id) or {}
    aliases = []
    for existing_alias in list(existing.get("receiver_aliases", [])) + list(profile["receiver_aliases"]):
        if existing_alias not in aliases:
            aliases.append(existing_alias)
    if alias and alias not in aliases:
        aliases.append(alias)
    profile["receiver_aliases"] = aliases[-MAX_ALIASES:]

    # An observed receiver identity maps to exactly one Spotify account. Never
    # steal an alias from another grant; require an explicit disconnect first.
    if alias:
        previous_id = store["aliases"].get(alias)
        if previous_id and previous_id != account_id:
            raise AliasCollisionError("receiver alias is already linked to another Spotify account")
        # A malformed on-disk collision is deliberately absent from the alias
        # index, so check the profile lists as well.
        if any(
            alias in existing.get("receiver_aliases", []) and existing_id != account_id
            for existing_id, existing in store["profiles"].items()
        ):
            raise AliasCollisionError("receiver alias has an ambiguous Spotify account mapping")

    store["profiles"][account_id] = profile
    return normalize_store(store)


def remove_profile(store, account_id):
    store = normalize_store(store)
    account_id = normalize_identifier(account_id)
    if account_id:
        store["profiles"].pop(account_id, None)
    return normalize_store(store)


def public_profile(profile):
    """Return owner-safe metadata with all reusable credentials removed."""
    profile = normalize_profile(profile)
    if not profile:
        return None
    return {
        "account_id": profile["account_id"],
        "display_name": profile["display_name"],
        "kind": profile["kind"],
        "connected_at": profile["connected_at"],
        "expires_at": profile["expires_at"],
        "scopes": list(profile["scopes"]),
        "receiver_alias_count": len(profile["receiver_aliases"]),
    }
