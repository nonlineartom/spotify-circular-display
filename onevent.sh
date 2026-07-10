#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Raspotify / librespot onevent handler
# Writes playback state to the systemd-managed runtime directory so the
# Flask server can read it without needing Spotify OAuth.
#
# Configure in /etc/raspotify/conf:
#   LIBRESPOT_ONEVENT="/usr/local/bin/spotify-onevent.sh"
# ─────────────────────────────────────────────────────────────

set -u

STATE_FILE="${SPOTIFY_STATE_FILE:-/run/spotify-display/spotify-state.json}"
BARE_ID="${TRACK_ID:-}"
BARE_ID="${BARE_ID##*:}"
export BARE_ID STATE_FILE

python3 << 'PYEOF'
import fcntl
import json
import os
import tempfile
import time

state_file = os.environ.get("STATE_FILE", "/run/spotify-display/spotify-state.json")
state_dir = os.path.dirname(state_file)
if not os.path.isdir(state_dir):
    raise RuntimeError(
        f"runtime directory {state_dir!r} is missing; run setup.sh/systemd-tmpfiles"
    )
lock_path = os.path.join(state_dir, ".spotify-state.lock")
event = os.environ.get("PLAYER_EVENT", "")
bare_id = os.environ.get("BARE_ID", "")
duration_ms = os.environ.get("DURATION_MS", "")
position_ms = os.environ.get("POSITION_MS", "")
volume = os.environ.get("VOLUME", "")

old_umask = os.umask(0o007)
flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
lock_fd = os.open(lock_path, flags, 0o660)
with os.fdopen(lock_fd, "r+") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    state = {}
    try:
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        state_fd = os.open(state_file, read_flags)
        with os.fdopen(state_fd, "r") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
    except (OSError, ValueError):
        pass

    was_playing = bool(state.get("is_playing", False))

    if event in ("playing", "started"):
        is_playing = True
    elif event in (
        "paused", "stopped", "end_of_track", "unavailable",
        "session_disconnected", "network_down",
    ):
        is_playing = False
    else:
        # Metadata/unknown events preserve the last authoritative play state.
        is_playing = was_playing

    state["event"] = event
    state["timestamp"] = time.time()
    state["is_playing"] = is_playing

    if bare_id:
        state["track_id"] = bare_id
    if duration_ms:
        try:
            state["duration_ms"] = max(0, int(duration_ms))
        except ValueError:
            pass
    if position_ms:
        try:
            state["position_ms"] = max(0, int(position_ms))
        except ValueError:
            pass
    elif event == "stopped":
        state["position_ms"] = 0
    elif event == "end_of_track" and state.get("duration_ms"):
        state["position_ms"] = state["duration_ms"]
    if volume:
        try:
            vol_int = max(0, min(65535, int(volume)))
            state["volume_percent"] = (vol_int * 100 + 32767) // 65535
        except ValueError:
            pass

    fd, tmp_file = tempfile.mkstemp(prefix=".spotify-state-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_file, 0o660)
        os.replace(tmp_file, state_file)
    except Exception:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass
        raise
os.umask(old_umask)
PYEOF
