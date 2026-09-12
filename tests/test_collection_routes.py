"""The Vinyls API verifies catalog facts and preserves local/account boundaries."""

import copy
import json

import pytest
import requests

import collection_routes
import server
from test_library_owner import library_client


ALBUM_ID = "a" * 22
URI = f"spotify:album:{ALBUM_ID}"
VERIFIED = {
    "id": ALBUM_ID, "uri": URI, "title": "Catalog title", "subtitle": "Catalog artist",
    "image": "https://i.scdn.co/image/verified", "release_date": "1998-04",
    "release_date_precision": "month", "total_tracks": 11,
}
EPOCH = "library-epoch"
REMOTE = {"REMOTE_ADDR": "192.168.1.40"}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


@pytest.fixture
def collection_client(library_client, tmp_path, monkeypatch):
    web, _config = library_client
    path = tmp_path / "vinyls.json"
    path.write_text(json.dumps({"version": 1, "albums": [], "import_notes": {"keep": True}}))
    monkeypatch.setattr(server, "VINYLS_FILE", str(path))
    monkeypatch.setattr(server, "lookup_album", lambda identifier: dict(VERIFIED) if identifier == ALBUM_ID else None)
    monkeypatch.setattr(server, "get_client_token", lambda: "fake-catalog-token")
    return web, path


@pytest.mark.parametrize("method", ["search", "add"])
def test_collection_routes_require_owner_on_lan_before_catalog_access(collection_client, monkeypatch, method):
    web, path = collection_client
    before = path.read_bytes()
    monkeypatch.setattr(server, "lookup_album", lambda *_: pytest.fail("unauthorized catalog lookup"))
    response = (
        web.get("/api/vinyls/search", query_string={"q": URI, "profile_epoch": EPOCH}, environ_base=REMOTE)
        if method == "search" else
        web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH}, environ_base=REMOTE)
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert path.read_bytes() == before


@pytest.mark.parametrize("headers", [
    {"Origin": "https://unrelated.example"},
    {"Referer": "https://unrelated.example/collection"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_add_cannot_change_collection(collection_client, monkeypatch, headers):
    web, path = collection_client
    before = path.read_bytes()
    monkeypatch.setattr(server, "lookup_album", lambda *_: pytest.fail("cross-origin catalog lookup"))
    response = web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH}, headers=headers)
    assert response.status_code == 403 and path.read_bytes() == before


@pytest.mark.parametrize("epoch", [None, "stale-epoch", 4])
@pytest.mark.parametrize("method", ["search", "add"])
def test_stale_or_missing_epoch_rejects_work_before_catalog_calls(collection_client, monkeypatch, epoch, method):
    web, path = collection_client
    before = path.read_bytes()
    monkeypatch.setattr(server, "lookup_album", lambda *_: pytest.fail("stale catalog lookup"))
    response = (
        web.get("/api/vinyls/search", query_string={"q": URI, "profile_epoch": epoch})
        if method == "search" else
        web.post("/api/vinyls", json={"uri": URI, "profile_epoch": epoch})
    )
    assert response.status_code == 409 and response.get_json()["code"] == "profile_changed"
    assert path.read_bytes() == before


@pytest.mark.parametrize("query", ["", "x", "x" * 121, "hello\nworld"])
def test_search_query_bounds_are_enforced_before_network(collection_client, query):
    web, _path = collection_client
    assert web.get("/api/vinyls/search", query_string={"q": query, "profile_epoch": EPOCH}).status_code == 400


@pytest.mark.parametrize("query", [
    URI, f"https://open.spotify.com/album/{ALBUM_ID}?si=share-id",
    f"https://open.spotify.com/intl-en/album/{ALBUM_ID}",
])
def test_album_links_and_uris_resolve_via_verified_catalog_metadata(collection_client, query):
    web, _path = collection_client
    response = web.get("/api/vinyls/search", query_string={"q": query, "profile_epoch": EPOCH})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    item = response.get_json()["items"][0]
    assert item["title"] == VERIFIED["title"] and item["uri"] == URI
    assert item["in_collection"] is False
    assert response.get_json()["profile_epoch"] == EPOCH


@pytest.mark.parametrize("query", [
    "https://attacker.example/album/" + ALBUM_ID,
    "https://open.spotify.com.attacker.example/album/" + ALBUM_ID,
    "https://open.spotify.com@attacker.example/album/" + ALBUM_ID,
    "https://open.spotify.com/playlist/" + ALBUM_ID,
    "spotify:track:" + ALBUM_ID,
    "spotify:album:../../me",
])
def test_arbitrary_links_cannot_proxy_requests_with_catalog_token(collection_client, query):
    web, _path = collection_client
    assert web.get("/api/vinyls/search", query_string={"q": query, "profile_epoch": EPOCH}).status_code == 400


def test_album_search_sanitizes_results_deduplicates_and_uses_fixed_spotify_origin(collection_client, monkeypatch):
    web, _path = collection_client
    calls = []
    raw = {
        "id": ALBUM_ID, "name": "Catalog result", "uri": "spotify:album:client-cannot-control-this",
        "artists": [{"name": "Artist"}, None, {"name": ["wrong shape"]}],
        "images": [{"url": "https://attacker.example/cover"}],
        "access_token": "must-not-escape", "release_date": "2001-03-14", "release_date_precision": "day",
    }

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"albums": {"items": [None, raw, dict(raw), {"id": "../bad", "name": "Bad"}, {"id": "b" * 22, "name": []}]}})

    monkeypatch.setattr(server._http, "get", fetch)
    response = web.get("/api/vinyls/search", query_string={"q": "album artist", "profile_epoch": EPOCH})
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 1 and items[0]["uri"] == URI
    assert items[0]["subtitle"] == "Artist" and items[0]["image"] == ""
    assert "must-not-escape" not in response.get_data(as_text=True)
    assert len(calls) == 1 and calls[0][0] == server.SPOTIFY_API_BASE + "/search"
    assert calls[0][1]["params"]["type"] == "album" and calls[0][1]["params"]["limit"] <= 10


@pytest.mark.parametrize("payload,status", [([], 200), ({"albums": {}}, 200), ({}, 403), ({}, 429)])
def test_upstream_search_failure_returns_retryable_response(collection_client, monkeypatch, payload, status):
    web, _path = collection_client
    monkeypatch.setattr(server._http, "get", lambda *_a, **_k: Response(payload, status))
    assert web.get("/api/vinyls/search", query_string={"q": "album artist", "profile_epoch": EPOCH}).status_code == 503


def test_search_network_failure_keeps_existing_collection(collection_client, monkeypatch):
    web, path = collection_client
    before = path.read_bytes()

    def unavailable(*_args, **_kwargs):
        raise requests.Timeout("offline")

    monkeypatch.setattr(server._http, "get", unavailable)
    assert web.get("/api/vinyls/search", query_string={"q": "album artist", "profile_epoch": EPOCH}).status_code == 503
    assert path.read_bytes() == before


def test_add_trusts_only_server_metadata_and_preserves_import_notes(collection_client):
    web, path = collection_client
    response = web.post("/api/vinyls", json={
        "uri": URI, "profile_epoch": EPOCH, "title": "Client title", "subtitle": "Client artist",
        "image": "https://attacker.example/image", "release_date": "1000", "availability": "available",
        "refresh_token": "must-not-be-stored", "verified_on": "1000-01-01", "market": "XX",
    })
    assert response.status_code == 201 and response.get_json()["added"] is True
    stored = json.loads(path.read_text())
    assert stored["import_notes"] == {"keep": True}
    item = stored["albums"][0]
    assert item["title"] == VERIFIED["title"] and item["subtitle"] == VERIFIED["subtitle"]
    assert item["image"] == VERIFIED["image"] and item["uri"] == URI
    assert item["spotify_release_date"] == VERIFIED["release_date"]
    assert item["spotify_total_tracks"] == 11 and item["market"] == "GB"
    assert "availability" not in item and "refresh_token" not in item
    assert item["verified_on"] != "1000-01-01"


def test_duplicate_add_does_not_rewrite_edition_or_invalidate_cache(collection_client, monkeypatch):
    web, path = collection_client
    raw = json.dumps({"albums": [{**VERIFIED, "title": "Original pressing", "edition_note": "Signed sleeve"}]}).encode()
    path.write_bytes(raw)
    before = dict(server._profile_generations)
    monkeypatch.setattr(collection_routes, "refresh_vinyl_crates", lambda *_: pytest.fail("duplicate must not invalidate the crate"))
    response = web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH})
    assert response.status_code == 200 and response.get_json()["added"] is False
    assert response.get_json()["item"]["title"] == "Original pressing"
    assert response.get_json()["item"]["edition_note"] == "Signed sleeve"
    assert path.read_bytes() == raw and server._profile_generations == before
    result = web.get("/api/vinyls/search", query_string={"q": URI, "profile_epoch": EPOCH})
    assert result.get_json()["items"][0]["in_collection"] is True


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"albums":{}}', b'{"albums":[null]}'])
def test_corrupt_catalog_refuses_add_without_overwriting(collection_client, raw):
    web, path = collection_client
    path.write_bytes(raw)
    response = web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH})
    assert response.status_code == 409 and path.read_bytes() == raw


@pytest.mark.parametrize("method", ["search", "add"])
def test_account_handoff_during_metadata_lookup_rejects_results_and_writes(collection_client, monkeypatch, method):
    web, path = collection_client
    before = path.read_bytes()

    def handoff(_identifier):
        server._receiver_identity["epoch"] = "new-profile-epoch"
        return dict(VERIFIED)

    monkeypatch.setattr(server, "lookup_album", handoff)
    response = (
        web.get("/api/vinyls/search", query_string={"q": URI, "profile_epoch": EPOCH})
        if method == "search" else
        web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH})
    )
    assert response.status_code == 409 and response.get_json()["code"] == "profile_changed"
    assert path.read_bytes() == before and "Catalog title" not in response.get_data(as_text=True)


@pytest.mark.parametrize("payload", [None, [], "bad", {}, {"uri": "spotify:playlist:" + ALBUM_ID, "profile_epoch": EPOCH}])
def test_invalid_add_payload_cannot_lookup_or_write(collection_client, monkeypatch, payload):
    web, path = collection_client
    before = path.read_bytes()
    monkeypatch.setattr(server, "lookup_album", lambda *_: pytest.fail("invalid album lookup"))
    response = web.post("/api/vinyls", json=payload)
    assert response.status_code in (400, 409) and path.read_bytes() == before


def test_unverified_album_is_not_written(collection_client, monkeypatch):
    web, path = collection_client
    before = path.read_bytes()
    monkeypatch.setattr(server, "lookup_album", lambda *_: None)
    assert web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH}).status_code == 503
    assert path.read_bytes() == before


def test_successful_add_refreshes_shared_vinyls_but_preserves_private_sections(collection_client, monkeypatch):
    web, _path = collection_client
    # This test supplies caches for two authorized profiles directly; their
    # independent grant lifecycle is covered by the profile suite.
    monkeypatch.setattr(server, "_maybe_prune_expired_profiles", lambda: None)
    made_for_you = {"id": "made_for_you", "items": [{"uri": "spotify:playlist:mix", "title": "A personal mix"}]}
    private = {"id": "saved", "items": [{"uri": "spotify:album:private", "title": "Private record"}]}
    other_private = {"id": "yours", "items": [{"uri": "spotify:playlist:other", "title": "Other user's list"}]}
    payload_a = {"sections": [made_for_you, {"id": "vinyls", "items": []}, private], "made_for_you": {"status": "partial"}}
    payload_b = {"sections": [other_private]}
    original_a, original_b = copy.deepcopy(payload_a), copy.deepcopy(payload_b)
    caches = {
        "profile:test-account": {"built_at": 123, "payload": payload_a},
        "profile:other-account": {"built_at": 123, "payload": payload_b},
        "generic": {"built_at": 123, "payload": {"sections": []}},
    }
    monkeypatch.setattr(server, "_crate_caches", caches)
    monkeypatch.setattr(server, "_profile_generations", {"test-account": 2, "other-account": 4})
    monkeypatch.setattr(server, "_crate_building_key", "profile:building-account")
    response = web.post("/api/vinyls", json={"uri": URI, "profile_epoch": EPOCH})
    assert response.status_code == 201
    for cache in caches.values():
        sections = cache["payload"]["sections"]
        assert len([section for section in sections if section["id"] == "vinyls"]) == 1
        vinyls = next(section for section in sections if section["id"] == "vinyls")
        assert vinyls["items"][0]["uri"] == URI and cache["built_at"] == 0
    result_a = caches["profile:test-account"]["payload"]
    assert result_a["sections"] == [made_for_you, result_a["sections"][1], private]
    assert result_a["made_for_you"] == {"status": "partial"}
    assert caches["profile:other-account"]["payload"]["sections"][1] == other_private
    assert payload_a == original_a and payload_b == original_b
    assert server._profile_generations == {"test-account": 3, "other-account": 5, "building-account": 1}
