#!/usr/bin/env python3
"""GPIO button handler for Pi Display.

Each button is wired between a GPIO pin and GND.
Internal pull-ups are enabled so the pin reads HIGH when open, LOW when pressed.

Note: Play/pause, next, and previous controls require MPRIS support
(future enhancement). Volume control uses amixer for local audio adjustment.
"""

import subprocess
import time
import signal
import sys

try:
    import RPi.GPIO as GPIO
except ImportError:
    print("RPi.GPIO not available — GPIO buttons disabled.")
    sys.exit(0)

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


def amixer_volume(direction):
    """Adjust system volume using amixer."""
    try:
        if direction == "up":
            subprocess.run(
                ["amixer", "set", "Master", f"{VOLUME_STEP}%+"],
                capture_output=True, timeout=3,
            )
        elif direction == "down":
            subprocess.run(
                ["amixer", "set", "Master", f"{VOLUME_STEP}%-"],
                capture_output=True, timeout=3,
            )
    except Exception as e:
        print(f"Volume control error: {e}")


def handle_button(channel):
    """Handle a button press on the given GPIO channel."""
    action = BUTTONS.get(channel)
    if not action:
        return

    if action == "volume_up":
        amixer_volume("up")
    elif action == "volume_down":
        amixer_volume("down")
    elif action in ("play_pause", "next", "previous"):
        # TODO: Implement via playerctl/MPRIS when available
        print(f"Button: {action} — requires MPRIS (not yet implemented)")
        return

    print(f"Button: {action} (GPIO {channel})")


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
    print("Note: Volume uses amixer. Play/next/prev require MPRIS (future).")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
