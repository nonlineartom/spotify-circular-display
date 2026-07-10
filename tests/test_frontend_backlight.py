"""Static contracts for the single-file brightness UI and gesture engine."""

from html.parser import HTMLParser
from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "templates" / "index.html"
).read_text(encoding="utf-8")


class _IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids[attributes["id"]] = (tag, attributes)


def _ids():
    parser = _IdParser()
    parser.feed(INDEX)
    return parser.ids


def test_brightness_hud_is_mirrored_onto_the_left_rim():
    controls = _ids()
    for element_id in (
        "brightness-hud",
        "bright-backing",
        "bright-track",
        "bright-fill",
        "bright-knob",
        "bright-label",
    ):
        assert element_id in controls

    _, hud = controls["brightness-hud"]
    assert hud.get("role") == "status"
    assert hud.get("aria-label") == "Display brightness"
    assert "#bright-label { left: 152px; }" in INDEX
    assert "const BRIGHT_A0 = 125, BRIGHT_A1 = 235" in INDEX
    assert "BRIGHT_A0 + (pct / 100) * (BRIGHT_A1 - BRIGHT_A0)" in INDEX
    assert "brightArcPath(BRIGHT_A0, a)" in INDEX


def test_three_finger_promotion_is_exclusive_and_drains_contacts():
    start = INDEX.split("function startBrightnessGesture()", 1)[1].split(
        "function processBrightnessGesture", 1
    )[0]
    assert "ids.length !== 3" in start
    assert "abortMtGesture()" in start
    assert "cancelSwipe()" in start

    abort = INDEX.split("function abortMtGesture()", 1)[1].split(
        "function startBrightnessGesture", 1
    )[0]
    assert "setVolume(mtGesture.volBase)" in abort

    pointer_down = INDEX.split(
        'viewport.addEventListener("pointerdown"', 1
    )[1].split('viewport.addEventListener("pointermove"', 1)[0]
    assert pointer_down.index("activePointers.size === 3") < pointer_down.index(
        "state.browsing"
    )
    assert "gesture.moved.size === 3" in INDEX
    assert "Math.abs(dCy) >= BRIGHTNESS_START_PX" in INDEX
    assert "multiTouchDrain = activePointers.size > 0" in INDEX


def test_backlight_requests_use_semantic_latest_only_intents():
    assert 'fetch("/api/backlight", { cache: "no-store" })' in INDEX
    assert 'scheduleBrightnessMode(screenDimmed ? "idle" : "active", true)' in INDEX
    assert 'data.mode === "idle"' in INDEX
    assert "state.activeBrightness" in INDEX
    assert "brightPendingIntent" in INDEX
    assert "body: JSON.stringify(intent.command)" in INDEX
    assert "const BRIGHTNESS_RETRY_DELAYS_MS = [250, 750, 1500]" in INDEX
    assert "intent.revision === brightnessIntentRevision" in INDEX
    assert "intent.retryCount < BRIGHTNESS_RETRY_DELAYS_MS.length" in INDEX
    assert "report_id" not in INDEX.lower()
    assert "hidraw" not in INDEX.lower()


def test_wake_landing_stays_intercepted_until_all_contacts_release():
    assert "#dimmer.wake-drain" in INDEX
    assert "const wakeContactPointers = new Set()" in INDEX
    assert 'dimmer.classList.add("wake-drain")' in INDEX
    assert "wakeContactPointers.delete(e.pointerId)" in INDEX
    assert 'dimmer.classList.remove("wake-drain")' in INDEX
    assert 'dimmer.addEventListener("pointercancel", finishWakeContact)' in INDEX
    assert 'dimmer.addEventListener("lostpointercapture", finishWakeContact)' in INDEX
