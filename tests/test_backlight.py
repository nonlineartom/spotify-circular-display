import threading
import time

import pytest

import backlight
import server


def _add_hidraw(sysfs_root, name, vendor="00000712", product="0000000A"):
    device = sysfs_root / name / "device"
    device.mkdir(parents=True)
    (device / "uevent").write_text(
        f"DRIVER=hid-generic\nHID_ID=0003:{vendor}:{product}\n",
        encoding="ascii",
    )


def _wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_discovery_matches_only_exact_waveshare_vid_pid(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw2", product="0000000B")
    _add_hidraw(sysfs, "hidraw7")

    controller = backlight.BacklightController(sysfs_root=str(sysfs), dev_root=str(dev))

    assert controller._discover_device_paths() == [str(dev / "hidraw7")]


def test_safe_mapping_builds_only_fixed_report_and_complement():
    physical, level, report = backlight._logical_to_command(100, 80)
    assert physical == 80
    assert level == 200
    assert report == bytes((0x09, 0x08, 0xF7, 0xC8, 0x37))
    assert report[4] == report[3] ^ 0xFF

    physical, level, report = backlight._logical_to_command(10, 80)
    assert (physical, level) == (8, 20)
    assert report == bytes((0x09, 0x08, 0xF7, 0x14, 0xEB))


def test_three_amp_ceiling_cannot_be_raised_by_config():
    controller = backlight.BacklightController({"safe_max_percent": 100})
    assert controller.safe_max_percent == 80
    assert backlight._logical_to_command(100, controller.safe_max_percent)[1] == 200


@pytest.mark.parametrize("bad", [None, True, "50", -1, 101, 2.5])
def test_public_percent_rejects_non_numeric_or_out_of_range_values(bad):
    with pytest.raises(ValueError):
        backlight._quantize_logical(bad)


def test_worker_ramps_first_contact_and_writes_exact_reports(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw3")
    (dev / "hidraw3").touch()
    reports = []
    opened = []

    def opener(path, _flags):
        opened.append(path)
        return 31

    def writer(_fd, report):
        reports.append(report)
        return len(report)

    controller = backlight.BacklightController(
        {"initial_percent": 100, "safe_max_percent": 80, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=opener,
        writer=writer,
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        state = controller.status()
    finally:
        controller.stop()

    # Unknown/re-enumerated hardware starts at logical 10 (physical 8), then
    # advances in bounded logical steps rather than one high-draw jump.
    assert [report[3] for report in reports] == list(range(20, 201, 20))
    assert all(report[:3] == bytes((0x09, 0x08, 0xF7)) for report in reports)
    assert all(report[4] == report[3] ^ 0xFF for report in reports)
    assert opened == [str(dev / "hidraw3")] * len(reports)
    assert state["applied_percent"] == 100
    assert state["hardware_percent"] == 80
    assert state["available"] is True


def test_worker_coalesces_requests_arriving_during_a_write(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw4")
    (dev / "hidraw4").touch()
    reports = []
    first_write_entered = threading.Event()
    release_first_write = threading.Event()

    def writer(_fd, report):
        reports.append(report)
        if len(reports) == 1:
            first_write_entered.set()
            assert release_first_write.wait(1)
        return len(report)

    controller = backlight.BacklightController(
        {"initial_percent": 100, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 32,
        writer=writer,
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert first_write_entered.wait(1)
        controller.set_percent(20)
        controller.set_percent(30)
        controller.set_percent(40)
        release_first_write.set()
        assert _wait_for(lambda: controller.status()["pending"] is False)
    finally:
        release_first_write.set()
        controller.stop()

    # The worker honors only the latest request, then reaches it through safe
    # ramp steps; the superseded targets do not cause extra writes.
    assert [report[3] for report in reports] == [20, 40, 60, 80]


def test_idle_preserves_active_percent_and_active_restores_it():
    # Disabled avoids starting a hardware worker; the state-machine semantics
    # are identical and can be asserted without a device.
    controller = backlight.BacklightController({"enabled": False, "idle_percent": 10})
    assert controller.set_percent(73)["active_percent"] == 70

    idle = controller.set_idle()
    assert idle["mode"] == "idle"
    assert idle["desired_percent"] == 10
    assert idle["active_percent"] == 70

    active = controller.set_active()
    assert active["mode"] == "active"
    assert active["desired_percent"] == 70
    assert active["active_percent"] == 70


def test_idle_never_brightens_an_active_zero_setting():
    controller = backlight.BacklightController({"enabled": False, "idle_percent": 10})
    controller.set_percent(0)

    idle = controller.set_idle()
    active = controller.set_active()

    assert idle["mode"] == "idle"
    assert idle["percent"] == 0
    assert idle["desired_percent"] == 0
    assert idle["active_percent"] == 0
    assert active["desired_percent"] == 0


def test_reconnect_uses_first_contact_ceiling_then_resumes_ramp(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw5")
    (dev / "hidraw5").touch()
    successful_reports = []
    fail_next = threading.Event()

    def writer(_fd, report):
        if fail_next.is_set():
            fail_next.clear()
            raise FileNotFoundError("simulated re-enumeration")
        successful_reports.append(report)
        return len(report)

    controller = backlight.BacklightController(
        {
            "initial_percent": 100,
            "ramp_interval_ms": 100,
            "retry_interval_seconds": 0.5,
        },
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 33,
        writer=writer,
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        before_reconnect = len(successful_reports)

        fail_next.set()
        controller.start()  # Force a health/application write at the target.
        assert _wait_for(lambda: controller.status()["error"] == "device_disconnected")
        assert _wait_for(lambda: controller.status()["pending"] is False)
        reconnect_levels = [report[3] for report in successful_reports[before_reconnect:]]
    finally:
        controller.stop()

    # The failed 100-logical report is not accepted. Rediscovery restarts from
    # logical 10 and resumes bounded ten-point ramping.
    assert reconnect_levels == list(range(20, 201, 20))


def _reenumerate_same_hidraw(sysfs, name):
    entry = sysfs / name
    (entry / "device").rename(entry / "previous-device")
    _add_hidraw(sysfs, name)


def test_silent_same_basename_reenumeration_is_safe_before_upward_request(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw6")
    successful_reports = []
    controller = backlight.BacklightController(
        {"initial_percent": 90, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 34,
        writer=lambda _fd, report: successful_reports.append(report) or len(report),
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        before_change = len(successful_reports)
        _reenumerate_same_hidraw(sysfs, "hidraw6")
        controller.set_percent(100)
        assert _wait_for(lambda: controller.status()["pending"] is False)
        levels = [report[3] for report in successful_reports[before_change:]]
    finally:
        controller.stop()

    assert levels == list(range(20, 201, 20))


def test_at_rest_reenumeration_automatically_reapplies_desired_brightness(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw8")
    successful_reports = []
    controller = backlight.BacklightController(
        {"initial_percent": 100, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 35,
        writer=lambda _fd, report: successful_reports.append(report) or len(report),
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        before_change = len(successful_reports)
        _reenumerate_same_hidraw(sysfs, "hidraw8")
        assert _wait_for(lambda: len(successful_reports) > before_change)
        assert _wait_for(lambda: controller.status()["pending"] is False)
        levels = [report[3] for report in successful_reports[before_change:]]
    finally:
        controller.stop()

    assert levels == list(range(20, 201, 20))


def test_missing_hardware_keeps_desired_state_without_raising(tmp_path):
    controller = backlight.BacklightController(
        {"retry_interval_seconds": 0.5},
        sysfs_root=str(tmp_path / "missing"),
        dev_root=str(tmp_path / "dev"),
    )
    try:
        state = controller.set_percent(73)
        assert state["desired_percent"] == 70
        assert _wait_for(lambda: controller.status()["error"] == "device_not_found")
        state = controller.status()
    finally:
        controller.stop()

    assert state["available"] is False
    assert state["applied_percent"] is None
    assert state["desired_percent"] == 70
    assert state["pending"] is True


class _FakeBacklight:
    def __init__(self):
        self.calls = []

    def _state(self, percent=60, mode="active"):
        return {
            "percent": percent,
            "desired_percent": percent,
            "applied_percent": percent,
            "active_percent": 60,
            "hardware_percent": 48,
            "safe_max_percent": 80,
            "mode": mode,
            "available": True,
            "pending": False,
        }

    def status(self, refresh=False):
        self.calls.append(("status", refresh))
        return self._state()

    def set_percent(self, percent):
        self.calls.append(("percent", percent))
        return self._state(percent=percent)

    def set_idle(self):
        self.calls.append(("mode", "idle"))
        return self._state(percent=10, mode="idle")

    def set_active(self):
        self.calls.append(("mode", "active"))
        return self._state()


@pytest.fixture
def backlight_api(monkeypatch):
    fake = _FakeBacklight()
    monkeypatch.setattr(server, "_get_backlight_controller", lambda: fake)
    server.app.config.update(TESTING=True, SECRET_KEY="backlight-test")
    server._rate_buckets.clear()
    return server.app.test_client(), fake


def test_backlight_get_refreshes_presence(backlight_api):
    web, fake = backlight_api
    response = web.get("/api/backlight", environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert response.status_code == 200
    assert response.get_json()["safe_max_percent"] == 80
    assert fake.calls == [("status", True)]
    assert response.headers["Cache-Control"] == "no-store"


def test_backlight_post_is_loopback_only_even_for_owner(backlight_api):
    web, fake = backlight_api
    response = web.post(
        "/api/backlight",
        json={"percent": 70},
        headers={"Authorization": "Bearer owner-secret"},
        environ_base={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert response.status_code == 403
    assert fake.calls == []


def test_backlight_post_rejects_dns_rebinding_host_on_loopback(backlight_api):
    web, fake = backlight_api
    response = web.post(
        "/api/backlight",
        json={"percent": 70},
        headers={"Host": "rebind.example", "Origin": "http://rebind.example"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 403
    assert fake.calls == []


def test_long_local_backlight_gesture_does_not_hit_generic_rate_limit(backlight_api):
    web, fake = backlight_api
    responses = [web.post("/api/backlight", json={"percent": 70}) for _ in range(80)]
    assert all(response.status_code == 200 for response in responses)
    assert len(fake.calls) == 80


def test_backlight_post_accepts_percent_and_idle_restore_modes(backlight_api):
    web, fake = backlight_api
    assert web.post("/api/backlight", json={"percent": 70}).get_json()["percent"] == 70
    assert web.post("/api/backlight", json={"mode": "idle"}).get_json()["mode"] == "idle"
    assert web.post("/api/backlight", json={"mode": "active"}).get_json()["mode"] == "active"
    assert fake.calls == [("percent", 70), ("mode", "idle"), ("mode", "active")]


@pytest.mark.parametrize("body", [
    {},
    {"mode": "bootloader"},
    {"percent": 50, "mode": "active"},
    {"percent": 50, "report": [9, 8, 247, 250, 5]},
])
def test_backlight_post_rejects_ambiguous_or_raw_hid_input(backlight_api, body):
    web, fake = backlight_api
    response = web.post("/api/backlight", json=body)
    assert response.status_code == 400
    assert fake.calls == []
