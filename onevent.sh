#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Raspotify / librespot onevent handler
# Writes playback state to /tmp/spotify-state.json so the
# Flask server can read it without needing Spotify OAuth.
#
# Configure in /etc/raspotify/conf:
#   LIBRESPOT_ONEVENT="/home/admin/circle-pi-display/onevent.sh"
# ─────────────────────────────────────────────────────────────

STATE_FILE="/tmp/spotify-state.json"
TMP_FILE="${STATE_FILE}.tmp"

# Extract the bare track ID from a Spotify URI
# e.g. "spotify:track:6rqhFgbbKwnb9MLmUQDhG6" → "6rqhFgbbKwnb9MLmUQDhG6"
TRACK_ID_RAW="${TRACK_ID:-}"
BARE_ID="${TRACK_ID_RAW##*:}"

# Map librespot volume (0-65535) to percent (0-100)
if [ -n "$VOLUME" ]; then
  VOL_PCT=$(( (VOLUME * 100 + 32767) / 65535 ))
else
  VOL_PCT=""
fi

# Determine is_playing based on event type
case "$PLAYER_EVENT" in
  playing|started|preloading)
    IS_PLAYING="true"
    ;;
  paused|stopped|end_of_track|unavailable)
    IS_PLAYING="false"
    ;;
  changed)
    IS_PLAYING="true"
    ;;
  volume_set)
    # Volume change — read existing state to preserve is_playing
    if [ -f "$STATE_FILE" ]; then
      EXISTING_PLAYING=$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('is_playing','true'))" 2>/dev/null || echo "true")
      IS_PLAYING="$EXISTING_PLAYING"
    else
      IS_PLAYING="true"
    fi
    ;;
  seeked|position_correction)
    # Seek — read existing state to preserve is_playing
    if [ -f "$STATE_FILE" ]; then
      EXISTING_PLAYING=$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('is_playing','true'))" 2>/dev/null || echo "true")
      IS_PLAYING="$EXISTING_PLAYING"
    else
      IS_PLAYING="true"
    fi
    ;;
  *)
    IS_PLAYING="true"
    ;;
esac

# Build JSON — use python3 for reliable JSON encoding
python3 -c "
import json, time, sys

state = {}

# Try to read existing state to preserve fields not in this event
try:
    with open('$STATE_FILE', 'r') as f:
        state = json.load(f)
except:
    pass

# Update with new event data
state['event'] = '${PLAYER_EVENT}'
state['timestamp'] = time.time()
state['is_playing'] = $IS_PLAYING

if '${BARE_ID}':
    state['track_id'] = '${BARE_ID}'

if '${DURATION_MS:-}':
    try:
        state['duration_ms'] = int('${DURATION_MS}')
    except:
        pass

if '${POSITION_MS:-}':
    try:
        state['position_ms'] = int('${POSITION_MS}')
    except:
        pass

if '${VOL_PCT}':
    try:
        state['volume_percent'] = int('${VOL_PCT}')
    except:
        pass

with open('$TMP_FILE', 'w') as f:
    json.dump(state, f)
" 2>/dev/null

# Atomic rename
if [ -f "$TMP_FILE" ]; then
  mv "$TMP_FILE" "$STATE_FILE"
fi
