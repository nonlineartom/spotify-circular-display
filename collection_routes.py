"""Touch-friendly catalogue lookup and additions to the local Vinyls collection."""

import copy
import re
from datetime import date
from urllib.parse import urlparse

import requests
from flask import jsonify, request

from vinyl_library import ALBUM_URI, add_vinyl, load_vinyls, VinylCatalogError, VinylValidationError


ALBUM_ID = re.compile(r"[A-Za-z0-9]{22}\Z")


def album_id_from_input(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if ALBUM_URI.fullmatch(value):
        return value.rsplit(":", 1)[-1]
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.netloc == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) == 2 and parts[0] == "album" and ALBUM_ID.fullmatch(parts[1]):
            return parts[1]
    return None


def search_albums(backend, query):
    """Public catalogue search; never follow arbitrary input URLs with a token."""
    album_id = album_id_from_input(query)
    if album_id:
        album = backend.lookup_album(album_id)
        return [album] if album and album.get("title") else []
    if query.startswith(("http:", "https:", "spotify:")):
        raise VinylValidationError("Use a Spotify album link, or search by album and artist.")
    token = backend.get_client_token()
    if not token:
        raise ConnectionError("Spotify search is unavailable. Check the connection in Settings.")
    response = backend._http.get(
        f"{backend.SPOTIFY_API_BASE}/search",
        params={"q": query, "type": "album", "limit": 10, "market": "GB"},
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    if response.status_code == 429:
        raise ConnectionError("Spotify is busy. Wait a moment before searching again.")
    if response.status_code != 200:
        raise ConnectionError("Spotify search is unavailable. Try again shortly.")
    data = response.json()
    albums = data.get("albums") if isinstance(data, dict) else None
    raw_items = albums.get("items") if isinstance(albums, dict) else None
    if not isinstance(raw_items, list):
        raise ConnectionError("Spotify returned an incomplete search result. Try again.")
    items, seen = [], set()
    for raw in raw_items[:10]:
        if not isinstance(raw, dict):
            continue
        identifier, title = raw.get("id"), raw.get("name")
        if not isinstance(identifier, str) or not ALBUM_ID.fullmatch(identifier) or identifier in seen:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        artists = raw.get("artists") if isinstance(raw.get("artists"), list) else []
        artist = ", ".join(a["name"] for a in artists if isinstance(a, dict) and isinstance(a.get("name"), str))
        image = backend._first_image_url(raw.get("images"))
        if not image.startswith("https://i.scdn.co/image/"):
            image = ""
        item = {"id": identifier, "uri": f"spotify:album:{identifier}", "title": title[:500],
                "subtitle": artist[:500], "image": image, "type": "album"}
        release_date = raw.get("release_date")
        if isinstance(release_date, str) and re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", release_date):
            try:
                date.fromisoformat(release_date + ("-01-01" if len(release_date) == 4 else "-01" if len(release_date) == 7 else ""))
                item["release_date"] = release_date
            except ValueError:
                pass
        items.append(item)
        seen.add(identifier)
    return items


def refresh_vinyl_crates(backend, context):
    """Replace the shared section and invalidate any builders using the old file."""
    items = load_vinyls(backend.VINYLS_FILE)
    with backend._crate_build_lock:
        backend._account_generation += 1
        accounts = set(backend._profile_generations)
        accounts.update(key[8:] for key in backend._crate_caches if key.startswith("profile:"))
        if context.get("account_id"):
            accounts.add(context["account_id"])
        building = backend._crate_building_key or ""
        if building.startswith("profile:"):
            accounts.add(building[8:])
        for account_id in accounts:
            backend._profile_generations[account_id] = backend._profile_generations.get(account_id, 0) + 1
        for cache in backend._crate_caches.values():
            payload = cache.get("payload")
            if isinstance(payload, dict):
                payload = copy.deepcopy(payload)
                sections = [s for s in payload.get("sections", []) if s.get("id") != "vinyls"]
                sections.insert(1 if sections and sections[0].get("id") == "made_for_you" else 0,
                                {"id": "vinyls", "title": "Vinyls", "items": items})
                payload["sections"] = sections
                cache["payload"] = payload
            cache["built_at"] = 0


def register_collection_routes(app, backend):
    @app.get("/api/vinyls/search")
    @backend.owner_required
    def vinyls_search():
        if not backend._rate_limit(20, 60):
            response = jsonify({"error": "Please wait before searching again."})
            response.status_code = 429
            response.headers["Retry-After"] = "60"
            return response
        context = backend._library_request_context(request.args.get("profile_epoch"))
        if context is None:
            return backend._profile_changed_response()
        query = request.args.get("q", "").strip()
        if not 2 <= len(query) <= 120 or any(ord(char) < 32 for char in query):
            return jsonify({"error": "Enter an album or artist name (2–120 characters)."}), 400
        try:
            found = search_albums(backend, query)
        except VinylValidationError as error:
            return jsonify({"error": str(error)}), 400
        except ConnectionError as error:
            return jsonify({"error": str(error)}), 503
        except (requests.RequestException, ValueError):
            return jsonify({"error": "Spotify search is unavailable. Try again in a moment."}), 503
        if not backend._crate_context_is_current(context):
            return backend._profile_changed_response()
        known = {item["uri"] for item in load_vinyls(backend.VINYLS_FILE)}
        items = [{**item, "in_collection": item["uri"] in known} for item in found]
        return jsonify({"items": items, **backend._public_profile_context(context)})

    @app.post("/api/vinyls")
    @backend.owner_required
    def vinyls_add():
        data = backend._request_json_object()
        if not isinstance(data, dict):
            return jsonify({"error": "Choose an album to add."}), 400
        context = backend._library_request_context(data.get("profile_epoch"))
        if context is None:
            return backend._profile_changed_response()
        uri = data.get("uri")
        if not isinstance(uri, str) or not ALBUM_URI.fullmatch(uri):
            return jsonify({"error": "Choose a Spotify album from the search results."}), 400
        # Client-supplied names, images and availability flags are ignored.
        album = backend.lookup_album(uri.rsplit(":", 1)[-1])
        if not album or not album.get("title"):
            return jsonify({"error": "This album could not be verified with Spotify. Try again shortly."}), 503
        verified = {key: album.get(key, "") for key in ("uri", "title", "subtitle", "image")}
        verified.update({"verified_on": date.today().isoformat(), "market": "GB"})
        if album.get("release_date"):
            verified["spotify_release_date"] = album["release_date"]
        if album.get("total_tracks") is not None:
            verified["spotify_total_tracks"] = album["total_tracks"]
        try:
            with backend._crate_build_lock:
                if not backend._crate_context_is_current(context):
                    return backend._profile_changed_response()
                item, added = add_vinyl(backend.VINYLS_FILE, verified)
                if added:
                    refresh_vinyl_crates(backend, context)
        except VinylValidationError as error:
            return jsonify({"error": str(error)}), 400
        except VinylCatalogError as error:
            return jsonify({"error": str(error)}), 409
        except OSError:
            return jsonify({"error": "The Vinyls collection could not be saved. Your existing records are safe."}), 503
        return jsonify({"item": item, "added": added, **backend._public_profile_context(context)}), 201 if added else 200
