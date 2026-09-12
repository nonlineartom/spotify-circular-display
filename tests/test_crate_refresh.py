"""Explicit library refresh remains bounded, account-scoped and observable."""

import copy
import threading

import pytest

import server
from test_library_owner import library_client


EPOCH = "library-epoch"
NEW = {"sections": [{"id": "made_for_you", "items": [{"uri": "spotify:playlist:weekly", "title": "Discover Weekly"}]}]}


@pytest.fixture
def refresh_client(library_client, monkeypatch):
    web, path = library_client
    monkeypatch.setattr(server, "_crate_building_key", None)
    monkeypatch.setattr(server, "_maybe_prune_expired_profiles", lambda: None)
    return web, path


def finish_build():
    with server._crate_build_condition:
        assert server._crate_build_condition.wait_for(lambda: not server._crate_building, timeout=2)


def test_refresh_requires_owner_and_same_origin_before_starting_work(refresh_client, monkeypatch):
    web, _path = refresh_client
    before = copy.deepcopy(server._crate_caches)
    monkeypatch.setattr(server, "_rebuild_crate_async", lambda *_: pytest.fail("unauthorized refresh started work"))
    response = web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}, environ_base={"REMOTE_ADDR": "192.168.1.40"})
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    response = web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}, headers={"Origin": "https://elsewhere.example"})
    assert response.status_code == 403
    assert server._crate_caches == before


@pytest.mark.parametrize("body,status", [(None, 400), ([], 400), ({}, 409), ({"profile_epoch": "old"}, 409)])
def test_invalid_refresh_never_invalidates_the_current_cache(refresh_client, monkeypatch, body, status):
    web, _path = refresh_client
    before = copy.deepcopy(server._crate_caches)
    monkeypatch.setattr(server, "_rebuild_crate_async", lambda *_: pytest.fail("invalid refresh started work"))
    response = web.post("/api/crate/refresh", json=body)
    assert response.status_code == status
    assert server._crate_caches == before


def test_refresh_returns_stale_rows_with_building_flag_until_fresh_results_publish(refresh_client, monkeypatch):
    web, path = refresh_client
    started, release = threading.Event(), threading.Event()
    previous = copy.deepcopy(server._crate_caches["profile:test-account"]["payload"])
    unchanged_config = path.read_bytes()
    other_cache = {"built_at": 123, "payload": {"sections": [{"id": "private-other", "items": []}]}}
    server._crate_caches["profile:other-account"] = copy.deepcopy(other_cache)
    monkeypatch.setattr(server, "_user_tokens", {"test-account": {"access_token": "unchanged-token"}})
    metadata_cache = server.BoundedTTLCache(10, 60)
    metadata_cache["record"] = {"label": "Keep this cached fact"}
    monkeypatch.setattr(server, "_album_cache", metadata_cache)

    def build(account, epoch):
        assert account == "test-account" and epoch == EPOCH
        started.set()
        assert release.wait(2)
        return copy.deepcopy(NEW)

    monkeypatch.setattr(server, "_build_crate_payload", build)
    try:
        response = web.post("/api/crate/refresh", json={"profile_epoch": EPOCH})
        assert response.status_code == 202 and response.get_json()["building"] is True
        assert response.headers["Cache-Control"] == "no-store"
        assert started.wait(1)
        waiting = web.get("/api/crate").get_json()
        assert waiting["building"] is True and waiting["sections"] == previous["sections"]
    finally:
        release.set()
        finish_build()
    refreshed = web.get("/api/crate").get_json()
    assert not refreshed.get("building") and refreshed["sections"] == NEW["sections"]
    assert server._crate_caches["profile:other-account"] == other_cache
    assert server._user_tokens == {"test-account": {"access_token": "unchanged-token"}}
    assert metadata_cache["record"] == {"label": "Keep this cached fact"}
    assert path.read_bytes() == unchanged_config


def test_refresh_rate_limit_allows_three_requests_per_minute_without_extra_builders(refresh_client, monkeypatch):
    web, _path = refresh_client
    calls = []
    monkeypatch.setattr(server, "_rebuild_crate_async", lambda context: calls.append(context))
    for _ in range(3):
        assert web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}).status_code == 202
    serial = server._crate_caches["profile:test-account"]["refresh_serial"]
    assert web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}).status_code == 429
    assert len(calls) == 3
    assert server._crate_caches["profile:test-account"]["refresh_serial"] == serial


def test_refresh_during_existing_build_waits_for_a_build_started_after_the_click(refresh_client, monkeypatch):
    web, _path = refresh_client
    started, release = threading.Event(), threading.Event()
    calls = []

    def build(_account, _epoch):
        calls.append(True)
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
            return {"sections": [{"id": "outdated", "items": []}]}
        return copy.deepcopy(NEW)

    monkeypatch.setattr(server, "_build_crate_payload", build)
    server._rebuild_crate_async()
    assert started.wait(1)
    try:
        assert web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}).status_code == 202
        assert web.get("/api/crate").get_json()["building"] is True
        assert len(calls) == 1
    finally:
        release.set()
        finish_build()
    cache = server._crate_caches["profile:test-account"]
    assert cache["refresh_requested"] is True
    assert all(section["id"] != "outdated" for section in cache["payload"]["sections"])
    web.get("/api/crate")  # The polling request starts the queued refresh.
    finish_build()
    result = web.get("/api/crate").get_json()
    assert len(calls) == 2 and result["sections"] == NEW["sections"]
    assert not result.get("building")


def test_refresh_queued_behind_another_accounts_builder_stays_observable(refresh_client, monkeypatch):
    web, _path = refresh_client
    monkeypatch.setattr(server, "_crate_building", True)
    monkeypatch.setattr(server, "_crate_building_key", "profile:other-account")
    monkeypatch.setattr(server, "_build_crate_payload", lambda *_: copy.deepcopy(NEW))
    assert web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}).status_code == 202
    assert web.get("/api/crate").get_json()["building"] is True
    assert server._crate_building_key == "profile:other-account"
    with server._crate_build_condition:
        server._crate_building = False
        server._crate_building_key = None
    web.get("/api/crate")
    finish_build()
    assert web.get("/api/crate").get_json()["sections"] == NEW["sections"]


def test_handoff_during_refresh_discards_old_account_results(refresh_client, monkeypatch):
    web, _path = refresh_client
    started, release = threading.Event(), threading.Event()

    def build(_account, _epoch):
        started.set()
        assert release.wait(2)
        return {"sections": [{"id": "private-old-account", "items": []}]}

    monkeypatch.setattr(server, "_build_crate_payload", build)
    try:
        assert web.post("/api/crate/refresh", json={"profile_epoch": EPOCH}).status_code == 202
        assert started.wait(1)
        server._receiver_identity.update(alias="unknown-account", epoch="new-epoch")
    finally:
        release.set()
        finish_build()
    cache = server._crate_caches["profile:test-account"]
    assert all(section["id"] != "private-old-account" for section in cache["payload"]["sections"])
    result = web.get("/api/crate").get_json()
    assert result["profile_epoch"] == "new-epoch"
    assert all(section["id"] != "private-old-account" for section in result["sections"])


def test_fast_refresh_returns_fresh_rows_with_completed_indicator(refresh_client, monkeypatch):
    web, _path = refresh_client
    cache = server._crate_caches["profile:test-account"]
    cache.update(built_at=0, refresh_requested=True)

    def immediate(context):
        current = server._crate_caches[context["cache_key"]]
        current.update(payload=copy.deepcopy(NEW), refresh_requested=False)

    monkeypatch.setattr(server, "_rebuild_crate_async", immediate)
    result = web.get("/api/crate").get_json()
    assert result["sections"] == NEW["sections"]
    assert not result.get("building")
