"""Keep test imports isolated from the operator's ignored local configuration."""

import os


os.environ.setdefault("FLASK_SECRET_KEY", "spotify-display-test-session-secret")
os.environ.setdefault("SPOTIFY_DISPLAY_DISABLE_BACKGROUND", "1")
