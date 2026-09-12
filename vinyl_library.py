"""Load the physical record collection without Spotify calls on the browse path."""

import fcntl
import json
import os
import re
import stat
import tempfile
import threading
from datetime import date


MAX_CATALOG_BYTES = 512 * 1024
MAX_VINYLS = 250
ALBUM_URI = re.compile(r"spotify:album:[A-Za-z0-9]{22}\Z")
_catalog_write_lock = threading.RLock()


class VinylValidationError(ValueError):
    """An album supplied for addition does not satisfy the catalog schema."""


class VinylCatalogError(ValueError):
    """The existing catalog cannot safely accept an addition."""


def load_vinyls(path):
    """Return verified album mappings only; keep import notes out of the UI."""
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            return []
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return []
    albums = data.get("albums") if isinstance(data, dict) else None
    if not isinstance(albums, list):
        return []

    items, seen = [], set()
    for album in albums[:MAX_VINYLS]:
        if not isinstance(album, dict):
            continue
        uri = album.get("uri")
        title, artist = album.get("title"), album.get("subtitle")
        if not isinstance(uri, str) or not ALBUM_URI.fullmatch(uri) or uri in seen:
            continue
        if not isinstance(title, str) or not title.strip() or not isinstance(artist, str):
            continue
        seen.add(uri)
        image = album.get("image")
        item = {
            "id": "vinyl-" + uri.rsplit(":", 1)[-1],
            "uri": uri,
            "title": title.strip()[:500],
            "subtitle": artist.strip()[:500],
            "image": image if isinstance(image, str) and image.startswith(("https://i.scdn.co/image/", "/static/")) else "",
            "accent": "#d8b96a",
            "type": "album",
        }
        if album.get("availability") == "unavailable":
            item["playable"] = False
            note = album.get("availability_note")
            item["availability_note"] = note[:500] if isinstance(note, str) else "This album is unavailable on Spotify."
        if isinstance(album.get("edition_note"), str):
            item["edition_note"] = album["edition_note"][:500]
        items.append(item)
    return items


def _album_text(value, field, maximum=500):
    if not isinstance(value, str) or not value.strip():
        raise VinylValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise VinylValidationError(f"{field} must contain at most {maximum} printable characters")
    return value


def _album_image(value):
    if not isinstance(value, str):
        raise VinylValidationError("image must be a string")
    if not value:
        return ""
    if len(value) > 2048:
        raise VinylValidationError("image is too long")
    if re.fullmatch(r"https://i\.scdn\.co/image/[A-Za-z0-9]+", value):
        return value
    if value.startswith("/static/") and re.fullmatch(r"/static/[A-Za-z0-9._/-]+", value):
        if all(part not in ("", ".", "..") for part in value[len("/static/"):].split("/")):
            return value
    raise VinylValidationError("image must be a Spotify artwork URL or local static asset")


def _album_release_date(value, field, partial=False):
    if not isinstance(value, str):
        raise VinylValidationError(f"{field} must be a date string")
    if partial and re.fullmatch(r"[0-9]{4}", value):
        candidate = value + "-01-01"
    elif partial and re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        candidate = value + "-01"
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        candidate = value
    else:
        raise VinylValidationError(f"{field} must be an ISO date")
    try:
        date.fromisoformat(candidate)
    except ValueError as error:
        raise VinylValidationError(f"{field} must be a valid date") from error
    return value


def _new_vinyl_entry(album):
    """Whitelist the new server-verified entry; never copy token/raw payload fields."""
    if not isinstance(album, dict):
        raise VinylValidationError("album must be an object")
    uri = album.get("uri")
    if not isinstance(uri, str) or not ALBUM_URI.fullmatch(uri):
        raise VinylValidationError("uri must identify a Spotify album with a 22-character ID")
    entry = {
        "id": "vinyl-" + uri.rsplit(":", 1)[-1],
        "uri": uri,
        "title": _album_text(album.get("title"), "title"),
        "subtitle": _album_text(album.get("subtitle"), "subtitle"),
        "image": _album_image(album.get("image", "")),
    }
    for field in ("spotify_title", "edition_note", "availability_note"):
        if field in album:
            entry[field] = _album_text(album[field], field)
    if "spotify_artists" in album:
        artists = album["spotify_artists"]
        if not isinstance(artists, list) or not 1 <= len(artists) <= 50:
            raise VinylValidationError("spotify_artists must contain 1–50 artist names")
        entry["spotify_artists"] = [_album_text(artist, "spotify_artists") for artist in artists]
    if "spotify_release_date" in album:
        entry["spotify_release_date"] = _album_release_date(album["spotify_release_date"], "spotify_release_date", partial=True)
    if "verified_on" in album:
        entry["verified_on"] = _album_release_date(album["verified_on"], "verified_on")
    if "spotify_total_tracks" in album:
        count = album["spotify_total_tracks"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10000:
            raise VinylValidationError("spotify_total_tracks must be an integer between 1 and 10000")
        entry["spotify_total_tracks"] = count
    if "market" in album:
        market = album["market"]
        if not isinstance(market, str) or not re.fullmatch(r"[A-Z]{2}", market):
            raise VinylValidationError("market must be a two-letter uppercase country code")
        entry["market"] = market
    if "availability" in album:
        if album["availability"] not in ("available", "unavailable"):
            raise VinylValidationError("availability must be available or unavailable")
        entry["availability"] = album["availability"]
    return entry


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _catalog_for_update(path):
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_CATALOG_BYTES + 1)
            mode = stat.S_IMODE(os.fstat(source.fileno()).st_mode)
    except FileNotFoundError:
        return {"version": 1, "title": "Vinyls", "albums": []}, 0o644
    if len(raw) > MAX_CATALOG_BYTES:
        raise VinylCatalogError("Vinyl catalog exceeds the 512 KiB size limit; no changes were made")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise VinylCatalogError("Vinyl catalog is malformed; repair it before adding records") from error
    if not isinstance(data, dict) or not isinstance(data.get("albums"), list) or any(not isinstance(entry, dict) for entry in data["albums"]):
        raise VinylCatalogError("Vinyl catalog must contain an albums list of objects; no changes were made")
    if len(data["albums"]) > MAX_VINYLS:
        raise VinylCatalogError("Vinyl catalog exceeds the 250-record limit; no changes were made")
    return data, mode


def _stored_vinyl_item(entry):
    """Use the existing browse representation without leaking import annotations."""
    uri, title, artist = entry.get("uri"), entry.get("title"), entry.get("subtitle")
    if not isinstance(title, str) or not title.strip() or not isinstance(artist, str):
        raise VinylCatalogError("The existing record has invalid title or artist metadata; repair it before adding it again")
    image = entry.get("image")
    item = {
        "id": "vinyl-" + uri.rsplit(":", 1)[-1],
        "uri": uri,
        "title": title.strip()[:500],
        "subtitle": artist.strip()[:500],
        "image": image if isinstance(image, str) and image.startswith(("https://i.scdn.co/image/", "/static/")) else "",
        "accent": "#d8b96a",
        "type": "album",
    }
    if entry.get("availability") == "unavailable":
        item["playable"] = False
        note = entry.get("availability_note")
        item["availability_note"] = note[:500] if isinstance(note, str) else "This album is unavailable on Spotify."
    if isinstance(entry.get("edition_note"), str):
        item["edition_note"] = entry["edition_note"][:500]
    return item


def add_vinyl(path, album):
    """Atomically add a verified album and return ``(browse_item, added)``.

    Spotify verification and authorization belong to the calling server route.
    This operation stores only a bounded local catalog entry. A stable sidecar
    lock covers read/modify/replace across processes; the in-process lock also
    serializes threads. Existing JSON metadata is preserved, and duplicate URIs
    return their stored representation without rewriting or replacing metadata.
    """
    entry = _new_vinyl_entry(album)
    path = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(path)
    with _catalog_write_lock:
        with open(path + ".lock", "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                catalog, mode = _catalog_for_update(path)
                for existing in catalog["albums"]:
                    if existing.get("uri") == entry["uri"]:
                        return _stored_vinyl_item(existing), False
                if len(catalog["albums"]) >= MAX_VINYLS:
                    raise VinylCatalogError("Vinyl catalog is full (250 records); no changes were made")
                catalog["albums"].append(entry)
                try:
                    encoded = (json.dumps(catalog, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise VinylCatalogError("Vinyl catalog contains invalid metadata; no changes were made") from error
                if len(encoded) > MAX_CATALOG_BYTES:
                    raise VinylCatalogError("Adding this record would exceed the 512 KiB catalog limit; no changes were made")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=".vinyls-", suffix=".tmp", delete=False) as target:
                        temporary = target.name
                        os.fchmod(target.fileno(), mode)
                        target.write(encoded)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, path)
                    temporary = None
                finally:
                    if temporary is not None:
                        try:
                            os.unlink(temporary)
                        except FileNotFoundError:
                            pass
                return _stored_vinyl_item(entry), True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
