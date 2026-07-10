#!/usr/bin/env python3
"""Optional GPIO controls for Raspberry Pi 4/5.

Buttons connect a BCM GPIO pin to ground and use internal pull-ups. The
RPi.GPIO-compatible API is provided by rpi-lgpio, which supports the Pi 5.
All playback and volume actions go through the local display API so they
control go-librespot's actual software volume rather than a non-existent Pi 5
analogue "Master" mixer.
"""

import os
import queue
import signal
import sys
import threading
import time

import requests

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError) as exc:
    print(f"spotify-buttons: GPIO backend unavailable: {exc}", file=sys.stderr)
    raise SystemExit(1)


BUTTONS = {
    17: "previous",
    27: "play_pause",
    22: "next",
    23: "volume_down",
    24: "volume_up",
}
DEBOUNCE_MS = max(50, min(2000, int(os.environ.get("GPIO_DEBOUNCE_MS", "250"))))
VOLUME_STEP = max(1, min(25, int(os.environ.get("GPIO_VOLUME_STEP", "5"))))
SERVER_URL = os.environ.get("SPOTIFY_DISPLAY_URL", "http://127.0.0.1:5000").rstrip("/")

_stop = threading.Event()
_actions = queue.Queue(maxsize=16)
_volume_percent = 50


def _request(method, path, payload=None):
    response = requests.request(
        method, f"{SERVER_URL}{path}", json=payload, timeout=3
    )
    if response.status_code >= 400:
        detail = response.text.strip()[:160]
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    return response


def _refresh_volume():
    global _volume_percent
    response = _request("GET", "/api/now-playing")
    if response.status_code == 204:
        return _volume_percent
    payload = response.json()
    try:
        _volume_percent = max(0, min(100, int(payload.get("volume_percent"))))
    except (AttributeError, TypeError, ValueError):
        pass
    return _volume_percent


def _perform(action):
    global _volume_percent
    if action in ("previous", "next", "play_pause"):
        api_action = "play-pause" if action == "play_pause" else action
        _request("POST", f"/api/control/{api_action}")
        return
    current = _refresh_volume()
    delta = VOLUME_STEP if action == "volume_up" else -VOLUME_STEP
    target = max(0, min(100, current + delta))
    _request("POST", "/api/control/volume", {"percent": target})
    _volume_percent = target


def _worker():
    while not _stop.is_set():
        try:
            action = _actions.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _perform(action)
            print(f"spotify-buttons: {action}", flush=True)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"spotify-buttons: {action} failed: {exc}", file=sys.stderr, flush=True)


def handle_button(channel):
    action = BUTTONS.get(channel)
    if not action or _stop.is_set():
        return
    try:
        _actions.put_nowait(action)
    except queue.Full:
        print("spotify-buttons: action queue full; dropping press", file=sys.stderr)


def _shutdown(_signum=None, _frame=None):
    _stop.set()


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        for pin in BUTTONS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(
                pin, GPIO.FALLING, callback=handle_button, bouncetime=DEBOUNCE_MS
            )
    except (RuntimeError, ValueError) as exc:
        GPIO.cleanup()
        print(f"spotify-buttons: GPIO initialization failed: {exc}", file=sys.stderr)
        return 1

    worker = threading.Thread(target=_worker, daemon=True, name="button-actions")
    worker.start()
    print(f"spotify-buttons: active as uid={os.geteuid()}; pins={BUTTONS}", flush=True)
    try:
        while not _stop.wait(1.0):
            pass
    finally:
        _stop.set()
        GPIO.cleanup()
        worker.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
