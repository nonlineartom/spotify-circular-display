import json
import os
from pathlib import Path
import subprocess

import numpy as np

import wled_sync


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_wled_device_config_is_bounded_and_supports_per_device_tuning():
    devices = wled_sync._normalize_devices({
        "devices": [
            {
                "host": "wled.local",
                "name": "x" * 100,
                "pixel_count": 10**9,
                "reverse": True,
                "phase_offset": -0.25,
                "brightness": 9,
                "gamma": 0,
            },
            {"host": "wled.local", "pixel_count": 1},
            {"host": "https://not-a-host/path", "pixel_count": 1},
            {"host": "8.8.8.8", "pixel_count": 1},
        ]
    })
    assert len(devices) == 1
    device = devices[0]
    assert device["pixel_count"] == wled_sync.MAX_PIXELS_PER_DEVICE
    assert len(device["name"]) == wled_sync.MAX_DEVICE_NAME_LENGTH
    assert device["reverse"] is True
    assert device["phase_offset"] == 0.75
    assert device["brightness"] == 1.0
    assert device["gamma"] == 0.5


def test_fetch_now_playing_distinguishes_idle_error_and_active(monkeypatch):
    monkeypatch.setattr(
        wled_sync.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code=204),
    )
    assert wled_sync.fetch_now_playing().state == "idle"

    monkeypatch.setattr(
        wled_sync.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code=200, payload=ValueError("bad")),
    )
    assert wled_sync.fetch_now_playing().state == "error"

    payload = {
        "is_playing": True,
        "progress_ms": "1200",
        "item": {
            "id": "track",
            "duration_ms": "4000",
            "album": {"album_type": "single", "images": [{"url": "https://img"}]},
        },
    }
    monkeypatch.setattr(
        wled_sync.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code=200, payload=payload),
    )
    result = wled_sync.fetch_now_playing()
    assert result.state == "active"
    assert result.snapshot["progress_ms"] == 1200
    assert result.snapshot["is_single"] is True
    assert "monotonic_time" in result.snapshot


def test_playback_tracker_retains_last_good_only_during_failure_grace():
    tracker = wled_sync.PlaybackTracker(failure_grace_seconds=8)
    snapshot = {"track_id": "one"}
    assert tracker._apply_result(wled_sync.PlaybackFetch("active", snapshot), 100)[0] == "active"
    assert tracker._apply_result(wled_sync.PlaybackFetch("error", error="timeout"), 105)[0] == "degraded"
    assert tracker.latest() == snapshot
    assert tracker._apply_result(wled_sync.PlaybackFetch("error", error="timeout"), 109)[0] == "unavailable"
    assert tracker.latest() is None
    tracker._apply_result(wled_sync.PlaybackFetch("active", snapshot), 110)
    assert tracker._apply_result(wled_sync.PlaybackFetch("idle"), 111)[0] == "idle"
    assert tracker.latest() is None


def test_frame_options_and_reverse_are_deterministic():
    palette = [(255, 0, 0), (0, 255, 0)]
    normal = wled_sync.build_frame(palette, 8, 0.2, 0.5, 0)
    reverse = wled_sync.build_frame(
        palette, 8, 0.2, 0.5, 0, reverse=True
    )
    normal_pixels = np.frombuffer(normal, dtype=np.uint8).reshape(-1, 3)
    reverse_pixels = np.frombuffer(reverse, dtype=np.uint8).reshape(-1, 3)
    np.testing.assert_array_equal(reverse_pixels, normal_pixels[::-1])
    dim = np.frombuffer(
        wled_sync.build_frame(palette, 8, 0.2, 0.5, 0, brightness=0.5),
        dtype=np.uint8,
    )
    assert dim.max() <= 128


def test_pause_keeps_play_cadence_until_motor_and_crossfade_settle():
    config = {"play_fps": 30, "pause_fps": 1}
    assert wled_sync._render_fps(config, False, 0.8, 0, 1.0) == 30
    assert wled_sync._render_fps(config, False, 0.0, 0, 0.4) == 30
    assert wled_sync._render_fps(config, False, 0.0, 0, 1.0) == 1


def test_interrupted_crossfade_snapshots_the_visible_palette():
    red = [(255, 0, 0)]
    blue = [(0, 0, 255)]
    green = [(0, 255, 0)]
    visible, target, started = wled_sync._start_palette_crossfade(
        red, blue, 10.0, green, 10.5
    )
    assert visible == [(128, 0, 128)]
    assert target == green
    assert started == 10.5


def test_onevent_uses_locked_atomic_runtime_state(tmp_path):
    state_file = tmp_path / "spotify-state.json"
    env = os.environ | {
        "SPOTIFY_STATE_FILE": str(state_file),
        "PLAYER_EVENT": "playing",
        "TRACK_ID": "spotify:track:abc",
        "DURATION_MS": "1234",
        "POSITION_MS": "100",
        "VOLUME": "32768",
    }
    subprocess.run(["bash", str(ROOT / "onevent.sh")], env=env, check=True)
    state = json.loads(state_file.read_text())
    assert state["track_id"] == "abc"
    assert state["is_playing"] is True
    env["PLAYER_EVENT"] = "network_down"
    subprocess.run(["bash", str(ROOT / "onevent.sh")], env=env, check=True)
    assert json.loads(state_file.read_text())["is_playing"] is False
    assert state_file.stat().st_mode & 0o777 == 0o660
    assert not list(tmp_path.glob(".spotify-state-*"))


def test_service_templates_render_without_tokens_or_graphical_cycle():
    replacements = {
        "@APP_USER@": "pi",
        "@APP_GROUP@": "spotify-display",
        "@APP_HOME@": "/home/pi",
        "@PROJECT_DIR@": "/home/pi/My Display",
        "@PROJECT_DIR_SYSTEMD@": "/home/pi/My\\x20Display",
        "@DISPLAY_PORT@": "5000",
    }
    for path in (ROOT / "services").glob("*.service"):
        rendered = path.read_text()
        for old, new in replacements.items():
            rendered = rendered.replace(old, new)
        assert "@APP_" not in rendered
        assert "@PROJECT_DIR@" not in rendered
        assert "@PROJECT_DIR_SYSTEMD@" not in rendered
        assert "@DISPLAY_PORT@" not in rendered
        if path.name in {"spotify-kiosk.service", "spotify-pygame.service"}:
            assert "After=graphical-session.target" not in rendered
            assert 'ExecStart="/home/pi/My Display/' in rendered
    path_unit = (ROOT / "services" / "spotify-wled.path").read_text()
    path_unit = path_unit.replace(
        "@PROJECT_DIR_SYSTEMD@", "/home/pi/My\\x20Display"
    )
    assert "@PROJECT_DIR@" not in path_unit
    assert "@PROJECT_DIR_SYSTEMD@" not in path_unit
    assert "PathChanged=/home/pi/My\\x20Display/config.json" in path_unit


def test_wled_launcher_exits_before_python_when_disabled(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"wled":{"enabled":false}}')
    result = subprocess.run(
        ["bash", str(ROOT / "wled-launch.sh")],
        env=os.environ | {"WLED_CONFIG_FILE": str(config)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert "dormant" in result.stdout


def test_setup_render_service_runs_under_nounset_with_space_in_path(tmp_path):
    setup_source = (ROOT / "setup.sh").read_text()
    function_body = setup_source.split("render_service() {", 1)[1].split(
        "\n}\n\nstep", 1
    )[0]
    runner = tmp_path / "render-test.sh"
    runner.write_text(
        "set -u\n"
        "sudo() { \"$@\"; }\n"
        f"render_service() {{{function_body}\n}}\n"
        'render_service "$SOURCE" "$DESTINATION"\n'
    )
    destination = tmp_path / "rendered.service"
    env = os.environ | {
        "TEMP_ROOT": str(tmp_path),
        "PROJECT_DIR": "/home/pi/My Display",
        "APP_USER": "pi",
        "APP_GROUP": "spotify-display",
        "APP_HOME": "/home/pi",
        "DISPLAY_PORT": "5001",
        "SOURCE": str(ROOT / "services" / "spotify-display.service"),
        "DESTINATION": str(destination),
    }
    subprocess.run(["bash", str(runner)], env=env, check=True)
    rendered = destination.read_text()
    assert "@PROJECT_DIR@" not in rendered
    assert "@PROJECT_DIR_SYSTEMD@" not in rendered
    assert "WorkingDirectory=/home/pi/My\\x20Display" in rendered
    assert 'ExecStart="/home/pi/My Display/serve.sh"' in rendered
    assert "Environment=PORT=5001" in rendered


def test_renderer_escapes_single_path_directives_for_systemd(tmp_path):
    rendered_dir = tmp_path / "rendered"
    subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "render_service_templates.py"),
            str(rendered_dir),
            "--app-user",
            "pi",
            "--app-group",
            "spotify-display",
            "--app-home",
            "/home/pi",
            "--project-dir",
            "/home/pi/My Display",
        ],
        check=True,
    )
    service = (rendered_dir / "spotify-display.service").read_text()
    path_unit = (rendered_dir / "spotify-wled.path").read_text()
    assert "WorkingDirectory=/home/pi/My\\x20Display" in service
    assert 'ExecStart="/home/pi/My Display/serve.sh"' in service
    assert "PathChanged=/home/pi/My\\x20Display/config.json" in path_unit


def test_watchdog_initial_sample_does_not_restart_receiver():
    source = (ROOT / "network_watchdog.sh").read_text()
    assert 'initial network state=$network_state; no boot-time restart performed' in source
    assert 'observed" != "$network_state' in source
    assert 'systemctl restart spotify-display' not in source
    assert "pkill -f" not in source


def test_graphical_launcher_waits_for_web_readiness_not_receiver_health():
    source = (ROOT / "display-launch.sh").read_text()
    assert '"${SPOTIFY_DISPLAY_URL}/api/info"' in source
    assert '"${SPOTIFY_DISPLAY_URL}/api/health"' not in source


def test_setup_requires_hashed_lockfile_for_dependencies():
    source = (ROOT / "setup.sh").read_text()
    assert "requirements.lock is missing" in source
    assert "--require-hashes" in source
    assert '-r "$PROJECT_DIR/requirements.lock"' in source
    assert "INSTALL_TEST_DEPS" in source
    assert "requirements-test.lock is missing" in source
    assert '-r "$PROJECT_DIR/requirements-test.lock"' in source
    assert "OS_PACKAGES+=(nodejs)" in source


def test_setup_does_not_stop_live_legacy_services_before_cutover():
    source = (ROOT / "setup.sh").read_text()
    assert "disable --now raspotify.service" not in source
    assert "rm -f /etc/systemd/system/spotify-kiosk.service" not in source


def test_setup_staged_install_preserves_service_and_host_policy_state():
    source = (ROOT / "setup.sh").read_text()
    assert 'STAGED_INSTALL="${STAGED_INSTALL:-0}"' in source
    assert 'case "$STAGED_INSTALL" in 0|1)' in source

    service_start = source.index(
        'if [ "$STAGED_INSTALL" = "1" ]; then\n'
        '    warn "Staged install: system service enable/disable state was preserved."'
    )
    service_end = source.index('\n\nstep "Installing graphical user service"', service_start)
    for mutation in (
        "sudo systemctl disable spotify-kiosk.service",
        "sudo systemctl disable raspotify.service",
        "sudo systemctl enable go-librespot",
        "sudo systemctl enable spotify-buttons",
        "sudo systemctl disable spotify-buttons",
    ):
        mutation_index = source.index(mutation)
        assert service_start < mutation_index < service_end

    graphical = source.split('step "Installing graphical user service"', 1)[1].split(
        'step "Hardening Wi-Fi for unattended recovery"', 1
    )[0]
    assert (
        'if [ "$STAGED_INSTALL" = "1" ]; then\n'
        '    warn "Staged install: graphical-session service links were preserved."\n'
        "else\n"
        in graphical
    )
    assert 'rm -f \\\n        "$USER_SYSTEMD_DIR/graphical-session.target.wants/' in graphical
    assert 'ln -s "../$USER_DISPLAY_SERVICE"' in graphical

    wifi = source.split('step "Hardening Wi-Fi for unattended recovery"', 1)[1].split(
        'step "Applying display power policy"', 1
    )[0]
    assert wifi.lstrip().startswith('if [ "$STAGED_INSTALL" = "1" ]; then')
    assert "harden-network.sh" in wifi

    display = source.split('step "Applying display power policy"', 1)[1]
    assert display.lstrip().startswith('if [ "$STAGED_INSTALL" = "1" ]; then')
    assert "raspi-config nonint do_blanking" in display
    assert "APPLY_LEGACY_HDMI_MODE" in display


def test_setup_only_configures_raspotify_when_fallback_is_requested():
    source = (ROOT / "setup.sh").read_text()
    config_guard = (
        'if [ "$INSTALL_RASPOTIFY_FALLBACK" = "1" ] '
        '&& id raspotify >/dev/null 2>&1; then'
    )
    config_start = source.index(config_guard)
    config_end = source.index('\n\nstep "Creating pinned Python environment"', config_start)
    config_block = source[config_start:config_end]

    assert source.count('/etc/raspotify/conf') == 1
    assert '/etc/raspotify/conf' in config_block
    assert 'sudo usermod -a -G "$APP_GROUP" raspotify' in source[:config_start]
    assert "any existing configuration was preserved" in config_block


def test_validation_does_not_lint_generated_virtualenv_scripts():
    source = (ROOT / "scripts" / "validate.sh").read_text()
    assert "-path './venv'" in source
    assert "-path './.venv'" in source
