import sys
import types


# The host audit runner does not install the optional graphical dependency;
# these polling-state tests never initialize or call pygame.
try:
    import pygame  # noqa: F401
except ModuleNotFoundError:
    _PYGAME_STUBBED = True
    sys.modules["pygame"] = types.ModuleType("pygame")
else:
    _PYGAME_STUBBED = False

import display

if _PYGAME_STUBBED:
    sys.modules.pop("pygame", None)


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _one_poll(monkeypatch, response):
    player = display.SpotifyVinyl.__new__(display.SpotifyVinyl)
    player.running = True
    updates = []
    errors = []
    player._update_state = updates.append
    player._log_poll_error = errors.append

    def request(*_args, **_kwargs):
        player.running = False
        return response

    monkeypatch.setattr(display.requests, "get", request)
    monkeypatch.setattr(display.time, "sleep", lambda _seconds: None)
    player._poll_loop()
    return updates, errors


def test_pygame_fallback_preserves_state_on_malformed_success(monkeypatch):
    updates, errors = _one_poll(monkeypatch, _Response(200, ["wrong-shape"]))

    assert updates == []
    assert errors == ["now-playing response is not an active playback object"]


def test_pygame_fallback_treats_http_204_as_authoritative_idle(monkeypatch):
    updates, errors = _one_poll(monkeypatch, _Response(204))

    assert updates == [None]
    assert errors == []
