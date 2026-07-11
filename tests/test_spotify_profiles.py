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
