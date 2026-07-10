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


def test_exact_device_discovery_and_fixed_safe_report(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw1", product="0000000B")
    _add_hidraw(sysfs, "hidraw7")

    controller = backlight.BacklightController(sysfs_root=str(sysfs), dev_root=str(dev))
    assert controller._discover_device_paths() == [str(dev / "hidraw7")]

    physical, level, report = backlight._logical_to_command(100, 80)
    assert (physical, level) == (80, 200)
    assert report == bytes((0x09, 0x08, 0xF7, 0xC8, 0x37))


def test_worker_ramps_first_contact_to_three_amp_ceiling(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw3")
    reports = []
    controller = backlight.BacklightController(
        {"initial_percent": 100, "safe_max_percent": 100, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 31,
        writer=lambda _fd, report: reports.append(report) or len(report),
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        state = controller.status()
    finally:
        controller.stop()

    assert [report[3] for report in reports] == list(range(20, 201, 20))
    assert state["applied_percent"] == 100
    assert state["hardware_percent"] == 80
    assert state["safe_max_percent"] == 80


@pytest.mark.parametrize("bad", [None, True, "50", -1, 101, 2.5])
def test_public_percent_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        backlight._quantize_logical(bad)


def test_worker_coalesces_requests_arriving_during_a_write(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw4")
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

    assert [report[3] for report in reports] == [20, 40, 60, 80]


def test_idle_preserves_active_brightness_for_restore():
    controller = backlight.BacklightController({"enabled": False, "idle_percent": 10})
    controller.set_percent(73)
    idle = controller.set_idle()
    active = controller.set_active()

    assert (idle["desired_percent"], idle["active_percent"], idle["mode"]) == (10, 70, "idle")
    assert (active["desired_percent"], active["active_percent"], active["mode"]) == (70, 70, "active")

    controller.set_percent(0)
    idle = controller.set_idle()
    active = controller.set_active()
    assert (idle["percent"], idle["desired_percent"], idle["active_percent"]) == (0, 0, 0)
    assert active["desired_percent"] == 0


def _reenumerate_same_hidraw(sysfs, name):
    entry = sysfs / name
    (entry / "device").rename(entry / "previous-device")
    _add_hidraw(sysfs, name)


@pytest.mark.parametrize("mutate_after", [True, False], ids=["before-upward-request", "at-rest"])
def test_same_basename_reenumeration_restarts_safe_ramp(tmp_path, mutate_after):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw8")
    reports = []
    initial = 90 if mutate_after else 100
    controller = backlight.BacklightController(
        {"initial_percent": initial, "ramp_interval_ms": 100},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=lambda _path, _flags: 35,
        writer=lambda _fd, report: reports.append(report) or len(report),
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        before_change = len(reports)
        _reenumerate_same_hidraw(sysfs, "hidraw8")
        if mutate_after:
            controller.set_percent(100)
        else:
            assert _wait_for(lambda: len(reports) > before_change)
        assert _wait_for(lambda: controller.status()["pending"] is False)
        reconnect_levels = [report[3] for report in reports[before_change:]]
    finally:
        controller.stop()

    assert reconnect_levels == list(range(20, 201, 20))


def test_reenumeration_between_discovery_and_write_discards_stale_report(tmp_path):
    sysfs = tmp_path / "sys" / "class" / "hidraw"
    dev = tmp_path / "dev"
    dev.mkdir(parents=True)
    _add_hidraw(sysfs, "hidraw9")
    reports = []
    change_contact = threading.Event()

    def opener(_path, _flags):
        if change_contact.is_set():
            change_contact.clear()
            _reenumerate_same_hidraw(sysfs, "hidraw9")
        return 36

    controller = backlight.BacklightController(
        {"initial_percent": 90, "ramp_interval_ms": 100, "retry_interval_seconds": 0.5},
        sysfs_root=str(sysfs),
        dev_root=str(dev),
        opener=opener,
        writer=lambda _fd, report: reports.append(report) or len(report),
        closer=lambda _fd: None,
    )
    try:
        controller.start()
        assert _wait_for(lambda: controller.status()["pending"] is False)
        before_change = len(reports)
        change_contact.set()
        controller.set_percent(100)
        assert _wait_for(lambda: controller.status()["pending"] is False)
        accepted_levels = [report[3] for report in reports[before_change:]]
    finally:
        controller.stop()

    assert accepted_levels == list(range(20, 201, 20))


def test_missing_hardware_keeps_desired_state_without_raising(tmp_path):
    controller = backlight.BacklightController(
        {"retry_interval_seconds": 0.5},
        sysfs_root=str(tmp_path / "missing"),
        dev_root=str(tmp_path / "dev"),
    )
    try:
        controller.set_percent(70)
        assert _wait_for(lambda: controller.status()["error"] == "device_not_found")
        state = controller.status()
    finally:
        controller.stop()

    assert state["available"] is False
    assert state["desired_percent"] == 70
    assert state["pending"] is True


class _FakeController:
    def __init__(self):
        self.calls = []

    def status(self, refresh=False):
        self.calls.append(("status", refresh))
        return {"desired_percent": 60, "active_percent": 60, "mode": "active"}

    def set_percent(self, value):
        self.calls.append(("percent", value))
        return {"desired_percent": value, "active_percent": value, "mode": "active"}

    def set_idle(self):
        self.calls.append(("mode", "idle"))
        return {"desired_percent": 10, "active_percent": 60, "mode": "idle"}

    def set_active(self):
        self.calls.append(("mode", "active"))
        return {"desired_percent": 60, "active_percent": 60, "mode": "active"}


@pytest.fixture
def api(monkeypatch):
    fake = _FakeController()
    monkeypatch.setattr(server, "_backlight_controller", fake)
    server.app.config["TESTING"] = True
    return server.app.test_client(), fake


def test_api_get_and_loopback_mutations(api):
    web, fake = api
    assert web.get("/api/backlight").get_json()["mode"] == "active"
    assert web.post("/api/backlight", json={"percent": 70}).get_json()["desired_percent"] == 70
    assert web.post("/api/backlight", json={"mode": "idle"}).get_json()["mode"] == "idle"
    assert web.post("/api/backlight", json={"mode": "active"}).get_json()["mode"] == "active"
    assert fake.calls == [
        ("status", True),
        ("percent", 70),
        ("mode", "idle"),
        ("mode", "active"),
    ]


def test_api_rejects_remote_or_raw_hid_mutation(api):
    web, fake = api
    remote = web.post(
        "/api/backlight",
        json={"percent": 70},
        environ_base={"REMOTE_ADDR": "192.168.1.10"},
    )
    raw = web.post(
        "/api/backlight",
        json={"percent": 70, "report": [9, 8, 247, 200, 55]},
    )
    rebound = web.post(
        "/api/backlight",
        json={"percent": 70},
        headers={"Host": "rebind.example", "Origin": "http://rebind.example"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert remote.status_code == 403
    assert raw.status_code == 400
    assert rebound.status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "bootloader"},
        {"percent": 50, "mode": "active"},
        {"percent": 50, "device": "/dev/hidraw0"},
    ],
)
def test_api_rejects_ambiguous_or_unsafe_payloads(api, body):
    web, fake = api
    response = web.post("/api/backlight", json=body)
    assert response.status_code == 400
    assert fake.calls == []
