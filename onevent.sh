#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Raspotify / librespot onevent handler
# Writes playback state to /tmp/spotify-state.json so the
# Flask server can read it without needing Spotify OAuth.
#
# Configure in /etc/raspotify/conf:
#   LIBRESPOT_ONEVENT="/usr/local/bin/spotify-onevent.sh"
# ─────────────────────────────────────────────────────────────

STATE_FILE="/tmp/spotify-state.json"
BARE_ID="${TRACK_ID##*:}"
export BARE_ID STATE_FILE

python3 << 'PYEOF'
import json, os, time

state_file = os.environ.get("STATE_FILE", "/tmp/spotify-state.json")
tmp_file = state_file + ".tmp"
event = os.environ.get("PLAYER_EVENT", "")
bare_id = os.environ.get("BARE_ID", "")
duration_ms = os.environ.get("DURATION_MS", "")
position_ms = os.environ.get("POSITION_MS", "")
volume = os.environ.get("VOLUME", "")

state = {}
try:
    with open(state_file, "r") as f:
        state = json.load(f)
except Exception:
    pass

if event in ("playing", "started", "preloading", "changed"):
    is_playing = True
elif event in ("paused", "stopped", "end_of_track", "unavailable"):
    is_playing = False
elif event in ("volume_set", "seeked", "position_correction"):
    is_playing = state.get("is_playing", True)
else:
    is_playing = True

state["event"] = event
state["timestamp"] = time.time()
state["is_playing"] = is_playing

if bare_id:
    state["track_id"] = bare_id
if duration_ms:
    try:
        state["duration_ms"] = int(duration_ms)
    except ValueError:
        pass
if position_ms:
    try:
        state["position_ms"] = int(position_ms)
    except ValueError:
        pass
if volume:
    try:
        vol_int = int(volume)
        state["volume_percent"] = (vol_int * 100 + 32767) // 65535
    except ValueError:
        pass

# Write with world-readable permissions
old_umask = os.umask(0o000)
with open(tmp_file, "w") as f:
    json.dump(state, f)
os.chmod(tmp_file, 0o644)
os.rename(tmp_file, state_file)
os.umask(old_umask)
PYEOF
