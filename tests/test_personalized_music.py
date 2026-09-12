"""Account-safe quick links and factual album details, with no network calls."""

import threading

import pytest

import server
from test_library_owner import ALBUM, TRACK, library_client  # shared private-boundary fixture


class Response:
    status_code = 200

    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def json(self):
        return self.data


def playlist(name, key="one", owner="spotify"):
    return {
        "name": name, "uri": f"spotify:playlist:{key}",
        "owner": {"id": owner, "display_name": "Spotify"}, "images": [],
    }


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(server, "_album_cache", server.BoundedTTLCache(20, 60))
    monkeypatch.setattr(server, "get_client_token", lambda: "catalog-token")
    return {
        "id": "record", "name": "A record", "artists": [{"name": "An artist"}],
        "images": [{"url": "https://i.scdn.co/image/cover"}],
        "album_type": "album", "total_tracks": 2,
        "release_date": "1997-06-16", "release_date_precision": "day",
    }


def test_album_catalog_lookup_provides_verified_record_facts_without_deprecated_fields(catalog, monkeypatch):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return Response(catalog)

    monkeypatch.setattr(server._http, "get", fetch)
    album = server.lookup_album("record")
    assert album["title"] == "A record" and album["subtitle"] == "An artist"
    assert album["release_date"] == "1997-06-16" and album["total_tracks"] == 2
    assert album["uri"] == "spotify:album:record" and album["album_type"] == "album"
    assert "label" not in album and "genres" not in album
    assert server.lookup_album("record") == album
    assert calls == [(f"{server.SPOTIFY_API_BASE}/albums/record", {"Authorization": "Bearer catalog-token"})]


@pytest.mark.parametrize("release,precision", [("1997", "year"), ("1997-06", "month"), ("1997-06-16", "day")])
def test_release_precision_is_preserved_without_inventing_a_month_or_day(catalog, monkeypatch, release, precision):
    monkeypatch.setattr(server._http, "get", lambda *_a, **_k: Response({**catalog, "release_date": release, "release_date_precision": precision}))
    result = server.lookup_album_metadata("record")
    assert result["release_date"] == release
    assert result["release_date_precision"] == precision


@pytest.mark.parametrize("bad", [
    {"release_date": "2026-02-30", "release_date_precision": "day"},
    {"release_date": "2026", "release_date_precision": "day"},
    {"release_date": [], "release_date_precision": []},
    {"release_date": "2026", "release_date_precision": ["year"]},
])
def test_unverifiable_dates_are_omitted(catalog, monkeypatch, bad):
    monkeypatch.setattr(server._http, "get", lambda *_a, **_k: Response({**catalog, **bad, "label": [], "total_tracks": True, "album_type": {}}))
    assert server.lookup_album_metadata("record") == {}


def test_complete_track_list_supplies_running_time_but_partial_list_does_not(catalog, monkeypatch):
    monkeypatch.setattr(server._http, "get", lambda *_a, **_k: Response({**catalog, "label": " Verified label "}))
    metadata = server.lookup_album_metadata("record", [dict(TRACK), dict(TRACK)])
    assert metadata["duration_ms"] == 360000
    assert metadata["label"] == "Verified label"
    assert "duration_ms" not in server.lookup_album_metadata("record", [dict(TRACK)])
    assert "duration_ms" not in server.lookup_album_metadata("record", [dict(TRACK), {**TRACK, "duration_ms": 0}])


def test_album_lookup_rejects_paths_and_mismatched_catalog_ids(catalog, monkeypatch):
    calls = []
    monkeypatch.setattr(server._http, "get", lambda *args, **_kwargs: calls.append(args) or Response({**catalog, "id": "another-record"}))
    assert server.lookup_album("../me") is None
    assert calls == []
    assert server.lookup_album("record") is None


def test_library_detail_includes_metadata_without_exposing_catalog_internal_fields(library_client, monkeypatch):
    web, _path = library_client
    monkeypatch.setattr(server, "lookup_album", lambda _album: {
        "release_date": "2020", "release_date_precision": "year", "total_tracks": 1,
        "private_marker": "not-a-browser-field", "label": "Catalog label",
    })
    response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
    assert response.status_code == 200
    assert response.get_json()["album_metadata"] == {
        "release_date": "2020", "release_date_precision": "year", "total_tracks": 1,
        "label": "Catalog label", "duration_ms": TRACK["duration_ms"],
    }
    assert "not-a-browser-field" not in response.get_data(as_text=True)


def test_metadata_lookup_cannot_finish_a_previous_accounts_selection(library_client, monkeypatch):
    web, _path = library_client

    def metadata(_album):
        server._receiver_identity["epoch"] = "different-account-epoch"
        return {"release_date": "2020", "release_date_precision": "year"}

    monkeypatch.setattr(server, "lookup_album", metadata)
    response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
    assert response.status_code == 409
    assert "album_metadata" not in response.get_json()


def test_album_detail_fetches_tracks_and_facts_together_then_computes_duration(library_client, monkeypatch):
    web, _path = library_client
    rendezvous = threading.Barrier(2)
    monkeypatch.setattr(server, "_album_cache", server.BoundedTTLCache(10, 60))
    monkeypatch.setattr(server, "_album_tracks_cache", server.BoundedTTLCache(10, 60))

    def tracks(_album):
        rendezvous.wait(timeout=2)
        return [dict(TRACK)]

    def metadata(_album):
        rendezvous.wait(timeout=2)
        return {"total_tracks": 1, "release_date": "2020", "release_date_precision": "year"}

    monkeypatch.setattr(server, "lookup_album_tracks", tracks)
    monkeypatch.setattr(server, "lookup_album", metadata)
    response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
    assert response.status_code == 200
    assert response.get_json()["tracks"] == [TRACK]
    assert response.get_json()["album_metadata"]["duration_ms"] == TRACK["duration_ms"]


def test_optional_metadata_cannot_hold_a_ready_track_list_until_network_returns(library_client, monkeypatch):
    web, _path = library_client
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(server, "_album_cache", server.BoundedTTLCache(10, 60))
    monkeypatch.setattr(server, "_album_tracks_cache", server.BoundedTTLCache(10, 60))
    monkeypatch.setattr(server, "ALBUM_DETAIL_METADATA_GRACE_SECONDS", .1)

    def slow_metadata(_album):
        started.set()
        try:
            assert release.wait(2)
            return {"total_tracks": 1}
        finally:
            finished.set()

    monkeypatch.setattr(server, "lookup_album", slow_metadata)
    try:
        response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
        assert started.is_set() and not finished.is_set()
        assert response.status_code == 200 and response.get_json()["tracks"] == [TRACK]
        assert response.get_json()["album_metadata"] == {}
    finally:
        release.set()
        assert finished.wait(2)


def test_shared_detail_deadline_returns_available_metadata_while_tracks_are_stalled(library_client, monkeypatch):
    web, _path = library_client
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(server, "_album_cache", server.BoundedTTLCache(10, 60))
    monkeypatch.setattr(server, "_album_tracks_cache", server.BoundedTTLCache(10, 60))
    monkeypatch.setattr(server, "ALBUM_DETAIL_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(server, "lookup_album", lambda _album: {"total_tracks": 1})

    def slow_tracks(_album):
        started.set()
        try:
            assert release.wait(2)
            return [dict(TRACK)]
        finally:
            finished.set()

    monkeypatch.setattr(server, "lookup_album_tracks", slow_tracks)
    try:
        response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
        data = response.get_json()
        assert started.is_set() and not finished.is_set()
        assert response.status_code == 200 and data["tracks_status"] == "unavailable"
        assert data["album_metadata"] == {"total_tracks": 1}
    finally:
        release.set()
        assert finished.wait(2)


def test_cached_details_need_no_worker_or_network_and_still_sanitize_tracks(library_client, monkeypatch):
    web, _path = library_client
    tracks_cache = server.BoundedTTLCache(10, 60)
    album_cache = server.BoundedTTLCache(10, 60)
    album_cache["album-one"] = {"total_tracks": 1}
    tracks_cache["album-one"] = [{**TRACK, "private": "not-for-browser"}]
    monkeypatch.setattr(server, "_album_tracks_cache", tracks_cache)
    monkeypatch.setattr(server, "_album_cache", album_cache)
    monkeypatch.setattr(server, "_submit_album_detail_read", lambda *_a: pytest.fail("cached detail submitted unnecessary work"))
    response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
    assert response.status_code == 200 and response.get_json()["tracks"] == [TRACK]
    assert response.get_json()["album_metadata"]["duration_ms"] == TRACK["duration_ms"]
    assert "not-for-browser" not in response.get_data(as_text=True)


def test_stalled_detail_jobs_cannot_create_an_unbounded_background_queue(monkeypatch):
    monkeypatch.setattr(server, "_album_detail_slots", threading.BoundedSemaphore(4))
    started = [threading.Event() for _ in range(4)]
    release = threading.Event()

    def stalled(index):
        started[index].set()
        assert release.wait(2)
        return index

    jobs, completed = [], []
    try:
        jobs = [server._submit_album_detail_read(stalled, i) for i in range(4)]
        assert all(job is not None for job in jobs)
        for job in jobs:
            event = threading.Event()
            job.add_done_callback(lambda _future, done=event: done.set())
            completed.append(event)
        assert all(event.wait(1) for event in started)
        assert all(server._submit_album_detail_read(stalled, 0) is None for _ in range(30))
    finally:
        release.set()
        for job in jobs:
            if job is not None:
                job.result(timeout=2)
        assert all(event.wait(2) for event in completed)
    assert server._submit_album_detail_read(lambda value: value, "ready").result(timeout=2) == "ready"


@pytest.mark.parametrize("name,owner,kind", [
    ("Discover Weekly", "spotify", "discover_weekly"),
    (" Release   Radar ", "spotify", "release_radar"),
    ("Daily Mix 2", "spotify", "mix"),
    ("Indie Mix", "spotify", "mix"),
    ("1990s Mix", "spotify", "mix"),
    ("Discover Weekly", "a-user-named-Spotify", None),
    ("Release Radar archive", "spotify", None),
    ("Mixing desk", "spotify", None),
    ("Daily Mix 8", "spotify", None),
])
def test_personalized_names_require_spotify_owner_id(name, owner, kind):
    assert server._personal_playlist_kind(playlist(name, owner=owner)) == kind


def test_playlist_discovery_reads_later_account_pages_without_following_next_url(monkeypatch):
    monkeypatch.setattr(server, "get_user_token", lambda *_a, **_k: "private-token")
    monkeypatch.setattr(server, "_profile_epoch_matches", lambda account, epoch: account == "listener" and epoch == "current")
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs["params"]))
        if kwargs["params"]["offset"] == 0:
            return Response({"items": [playlist(f"User list {i}", str(i), "listener") for i in range(50)], "next": "https://wrong-origin.example/token"})
        return Response({"items": [playlist("Discover Weekly", "weekly"), playlist("Release Radar", "radar"), playlist("Daily Mix 1", "mix"), playlist("Discover Weekly", "copy", "impostor")], "next": None})

    monkeypatch.setattr(server._http, "get", fetch)
    items = server.fetch_user_playlists(limit=500, account_id="listener", profile_epoch="current")
    selected, status = server._made_for_you_payload(items, "listener")
    assert [item["uri"] for item in selected] == ["spotify:playlist:weekly", "spotify:playlist:radar", "spotify:playlist:mix"]
    assert status["status"] == "ready" and status["missing"] == []
    assert all(url == f"{server.SPOTIFY_API_BASE}/me/playlists" for url, _params in calls)
    assert [params["offset"] for _url, params in calls] == [0, 50]


def test_account_handoff_drops_even_already_fetched_playlist_pages(monkeypatch):
    current = [True]
    monkeypatch.setattr(server, "get_user_token", lambda *_a, **_k: "private-token")
    monkeypatch.setattr(server, "_profile_epoch_matches", lambda *_a: current[0])

    def fetch(*_args, **_kwargs):
        current[0] = False
        return Response({"items": [playlist("Discover Weekly")]})

    monkeypatch.setattr(server._http, "get", fetch)
    assert server.fetch_user_playlists(account_id="old-listener", profile_epoch="old-epoch") == []


def test_no_global_playlist_fallback_when_personalized_playlists_are_absent():
    selected, status = server._made_for_you_payload([], "listener")
    assert selected == [] and status["status"] == "unavailable"
    assert status["missing"] == ["discover_weekly", "release_radar", "mix"]
    assert "Save Discover Weekly" in status["message"]
    selected, status = server._made_for_you_payload([{"uri": "spotify:playlist:previous", "quick_kind": "mix"}], None)
    assert selected == [] and status["status"] == "link_required"


def test_quick_link_selection_is_bounded_and_missing_kinds_remain_honest():
    selected, status = server._made_for_you_payload([
        {"uri": f"spotify:playlist:mix{i}", "quick_kind": "mix"} for i in range(50)
    ], "listener")
    assert len(selected) == 6 and status["status"] == "partial"
    assert status["missing"] == ["discover_weekly", "release_radar"]
