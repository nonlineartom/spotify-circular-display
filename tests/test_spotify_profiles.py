import datetime

import pytest

import spotify_profiles


def profile(account_id, alias, refresh_token="refresh-token"):
    return {
        "account_id": account_id,
        "display_name": account_id,
        "refresh_token": refresh_token,
        "kind": "owner",
        "connected_at": 1,
        "expires_at": None,
        "scopes": ["user-library-read"],
        "receiver_aliases": [alias],
    }


def test_identifiers_and_aliases_are_opaque_and_case_sensitive():
    store = spotify_profiles.normalize_store({
        "profiles": {
            "Account-A": profile("Account-A", "Receiver"),
            "account-a": profile("account-a", "receiver"),
        }
    })

    assert spotify_profiles.profile_for_alias(store, "Receiver")["account_id"] == "Account-A"
    assert spotify_profiles.profile_for_alias(store, "receiver")["account_id"] == "account-a"
    assert spotify_profiles.profile_for_alias(store, "RECEIVER") is None


def test_ambiguous_alias_fails_closed_and_cannot_be_stolen():
    ambiguous = spotify_profiles.normalize_store({
        "profiles": {
            "one": profile("one", "same"),
            "two": profile("two", "same"),
        }
    })

    assert spotify_profiles.profile_for_alias(ambiguous, "same") is None
    with pytest.raises(spotify_profiles.AliasCollisionError):
        spotify_profiles.upsert_profile(ambiguous, profile("three", "same"), "same")


def test_reauthorizing_an_account_merges_bounded_aliases():
    store = spotify_profiles.upsert_profile({}, profile("one", "old"), "old")
    replacement = profile("one", "new", refresh_token="rotated")
    store = spotify_profiles.upsert_profile(store, replacement, "new")

    saved = store["profiles"]["one"]
    assert saved["refresh_token"] == "rotated"
    assert saved["receiver_aliases"] == ["old", "new"]


def test_refresh_token_is_bounded_and_rejects_control_characters():
    too_large = "x" * (spotify_profiles.MAX_REFRESH_TOKEN_LENGTH + 1)
    assert spotify_profiles.normalize_profile(profile("one", "alias", too_large)) is None
    assert spotify_profiles.normalize_profile(profile("one", "alias", "token\nvalue")) is None


def test_new_profile_cannot_exceed_store_bound():
    store = {"profiles": {}}
    for index in range(spotify_profiles.MAX_PROFILES):
        item = profile(f"account-{index}", f"alias-{index}")
        store = spotify_profiles.upsert_profile(store, item, f"alias-{index}")

    with pytest.raises(spotify_profiles.ProfileLimitError):
        spotify_profiles.upsert_profile(
            store,
            profile("one-too-many", "alias-too-many"),
            "alias-too-many",
        )


def test_legacy_owner_migrates_to_household_without_inventing_authorization_time():
    migrated = spotify_profiles.normalize_profile(profile("one", "alias"))

    assert migrated["kind"] == "household"
    assert migrated["authorized_at"] is None
    assert migrated["reauthorize_at"] is None
    assert migrated["expires_at"] is None


def test_legacy_owner_migration_preserves_a_finite_expiry_cutoff():
    item = profile("one", "alias")
    item["expires_at"] = 1234

    migrated = spotify_profiles.normalize_profile(item)

    assert migrated["kind"] == "household"
    assert migrated["expires_at"] == 1234


def test_existing_guest_remains_bounded_during_store_migration():
    item = profile("one", "alias")
    item.update({"kind": "guest", "expires_at": 1234})

    migrated = spotify_profiles.normalize_profile(item)

    assert migrated["kind"] == "guest"
    assert migrated["expires_at"] == 1234
    assert migrated["authorized_at"] is None
    assert migrated["reauthorize_at"] is None


def test_six_month_deadline_uses_calendar_arithmetic_and_clamps_month_end():
    authorized = datetime.datetime(
        2025, 8, 31, 12, 30, tzinfo=datetime.timezone.utc
    ).timestamp()
    expected = datetime.datetime(
        2026, 2, 28, 12, 30, tzinfo=datetime.timezone.utc
    ).timestamp()

    assert spotify_profiles.reauthorization_deadline(authorized) == expected


def test_profile_lifecycle_is_bounded_and_public_metadata_never_contains_token():
    authorized = datetime.datetime(
        2026, 1, 31, tzinfo=datetime.timezone.utc
    ).timestamp()
    item = profile("one", "alias")
    item.update({
        "kind": "household",
        "authorized_at": authorized,
        # A malformed later deadline is capped to the calendar policy.
        "reauthorize_at": authorized + 400 * 24 * 60 * 60,
    })

    normalized = spotify_profiles.normalize_profile(item)
    public = spotify_profiles.public_profile(normalized)

    assert normalized["reauthorize_at"] == spotify_profiles.reauthorization_deadline(
        authorized
    )
    assert public["authorized_at"] == authorized
    assert public["reauthorize_at"] == normalized["reauthorize_at"]
    assert "refresh_token" not in public
