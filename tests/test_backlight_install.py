from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "setup.sh").read_text(encoding="utf-8")
SERVICE = (ROOT / "services" / "spotify-display.service").read_text(encoding="utf-8")
RULE = (ROOT / "udev" / "70-spotify-display-backlight.rules").read_text(
    encoding="utf-8"
)


def test_installer_creates_restricted_hid_access():
    assert 'BACKLIGHT_GROUP="spotify-backlight"' in SETUP
    assert "groupadd --system" in SETUP
    assert "70-spotify-display-backlight.rules" in SETUP
    assert "udevadm control --reload-rules" in SETUP
    assert "udevadm trigger --action=change --subsystem-match=hidraw" in SETUP
    assert "chmod 777" not in SETUP


def test_rule_and_service_target_only_the_waveshare_controller():
    assert 'ATTRS{idVendor}=="0712"' in RULE
    assert 'ATTRS{idProduct}=="000a"' in RULE
    assert 'GROUP:="spotify-backlight"' in RULE
    assert 'MODE:="0660"' in RULE
    assert "SupplementaryGroups=spotify-backlight" in SERVICE
