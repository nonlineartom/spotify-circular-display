#!/usr/bin/env python3
"""GPIO button handler for Pi Display.

Each button is wired between a GPIO pin and GND.
Internal pull-ups are enabled so the pin reads HIGH when open, LOW when pressed.
"""

import time
import signal
import sys
import requests
import RPi.GPIO as GPIO

SERVER_URL = "http://localhost:5000"

# GPIO pin → action mapping
BUTTONS = {
    17: "previous",
    27: "play_pause",
    22: "next",
    23: "volume_down",
    24: "volume_up",
}

DEBOUNCE_MS = 250
VOLUME_STEP = 5

current_volume = 50  # Will be updated from Spotify


def get_current_state():
    """Fetch current playback state from the local server."""
    global current_volume
    try:
        resp = requests.get(f"{SERVER_URL}/api/now-playing", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("device"):
                current_volume = data["device"].get("volume_percent", 50)
            return data
    except Exception:
        pass
    return None


def handle_button(channel):
    """Handle a button press on the given GPIO channel."""
    global current_volume
    action = BUTTONS.get(channel)
    if not action:
        return

    try:
        if action == "play_pause":
            state = get_current_state()
            if state and state.get("is_playing"):
                requests.put(f"{SERVER_URL}/api/pause", timeout=3)
            else:
                requests.put(f"{SERVER_URL}/api/play", timeout=3)

        elif action == "next":
            requests.post(f"{SERVER_URL}/api/next", timeout=3)

        elif action == "previous":
            requests.post(f"{SERVER_URL}/api/previous", timeout=3)

        elif action == "volume_up":
            current_volume = min(100, current_volume + VOLUME_STEP)
            requests.put(
                f"{SERVER_URL}/api/volume?volume_percent={current_volume}", timeout=3
            )

        elif action == "volume_down":
            current_volume = max(0, current_volume - VOLUME_STEP)
            requests.put(
                f"{SERVER_URL}/api/volume?volume_percent={current_volume}", timeout=3
            )

        print(f"Button: {action} (GPIO {channel})")

    except requests.exceptions.RequestException as e:
        print(f"Error handling {action}: {e}")


def cleanup(signum, frame):
    GPIO.cleanup()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    GPIO.setmode(GPIO.BCM)

    for pin in BUTTONS:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(
            pin, GPIO.FALLING, callback=handle_button, bouncetime=DEBOUNCE_MS
        )

    print(f"GPIO buttons active: {BUTTONS}")

    # Fetch initial volume
    get_current_state()

    # Keep running
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
