#!/usr/bin/env python3
"""Safe HID backlight control for the Waveshare 7-inch round display.

The panel's backlight command is an output report on the USB touch device, not
a Linux ``/sys/class/backlight`` control.  This module deliberately exposes
only logical brightness operations; callers cannot supply a device path,
report id, command byte, or raw payload.

The controller is output-only, so ``applied_percent`` is the last command that
the kernel accepted rather than a value queried back from the panel.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional


WAVESHARE_USB_VENDOR_ID = 0x0712
WAVESHARE_USB_PRODUCT_ID = 0x000A

# Report 9, four-byte payload: 08 F7 <level> <ones-complement>.
_REPORT_ID = 0x09
_COMMAND = (0x08, 0xF7)
_REPORT_LENGTH = 5

LOGICAL_STEP_PERCENT = 10
# Public brightness choices stay on predictable ten-point values, while the
# HID worker interpolates between them in one-point substeps. At the default
# cadence this produces small reports every 15 ms instead of visible ten-point
# jumps every 150 ms without changing the overall slew rate.
RAMP_STEP_PERCENT = 1
DEFAULT_SAFE_MAX_PERCENT = 80
# This build targets the existing Pi 5 3 A supply.  Configuration may lower
# this ceiling, but cannot silently raise it to the panel's highest draw.
THREE_AMP_SAFE_MAX_PERCENT = 80


class BacklightError(RuntimeError):
    """Base class for bounded, user-displayable backlight failures."""


class BacklightUnavailable(BacklightError):
    """The exact Waveshare HID device is absent or cannot be opened."""


def _bounded_number(value, default, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(float(minimum), min(float(maximum), number))


def _whole_logical_percent(value) -> int:
    """Validate one whole logical percentage without public quantization."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("percent must be a number from 0 to 100")
    if not float(value).is_integer():
        raise ValueError("percent must be a whole number from 0 to 100")
    percent = int(value)
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return percent


def _quantize_logical(value) -> int:
    """Validate and round a public 0--100 value to ten-percent steps."""
    percent = _whole_logical_percent(value)
    # Half-up makes the result predictable (Python's round uses bankers'
    # rounding at 5), while still accepting touch-derived integer values.
    return min(100, ((percent + LOGICAL_STEP_PERCENT // 2) // LOGICAL_STEP_PERCENT) * LOGICAL_STEP_PERCENT)


def _hid_id_matches(uevent_text: str) -> bool:
    """Return true only for HID_ID lines carrying the exact USB VID/PID."""
    for line in uevent_text.splitlines():
        if not line.startswith("HID_ID="):
            continue
        fields = line.partition("=")[2].split(":")
        if len(fields) != 3:
            return False
        try:
            vendor = int(fields[1], 16) & 0xFFFF
            product = int(fields[2], 16) & 0xFFFF
        except ValueError:
            return False
        return vendor == WAVESHARE_USB_VENDOR_ID and product == WAVESHARE_USB_PRODUCT_ID
    return False


def _usb_ancestor_matches(device_path: Path) -> bool:
    """Fallback for kernels/sysfs fixtures that omit HID_ID from uevent."""
    try:
        current = device_path.resolve()
    except OSError:
        current = device_path
    for parent in (current, *current.parents):
        vendor_path = parent / "idVendor"
        product_path = parent / "idProduct"
        if not vendor_path.is_file() or not product_path.is_file():
            continue
        try:
            vendor = int(vendor_path.read_text(encoding="ascii").strip(), 16)
            product = int(product_path.read_text(encoding="ascii").strip(), 16)
        except (OSError, UnicodeError, ValueError):
            continue
        return vendor == WAVESHARE_USB_VENDOR_ID and product == WAVESHARE_USB_PRODUCT_ID
    return False


def _sysfs_contact_identity(entry: Path) -> Optional[tuple[str, int, int]]:
    """Stable identity for one live HID contact, including sysfs object inode."""
    device = entry / "device"
    try:
        resolved = str(device.resolve(strict=True))
        stat = device.stat()
    except OSError:
        return None
    return resolved, int(stat.st_dev), int(stat.st_ino)


def _logical_to_command(logical_percent: int, safe_max_percent: int) -> tuple[int, int, bytes]:
    """Map a logical setting to physical percent, HID level, and fixed report."""
    # Public targets are quantized separately. Internal one-point ramp values
    # must reach the HID encoder intact or they collapse back into the visible
    # ten-point jumps the interpolation is intended to remove.
    logical = _whole_logical_percent(logical_percent)
    safe_max = max(0, min(THREE_AMP_SAFE_MAX_PERCENT, int(safe_max_percent)))

    # The Waveshare protocol's documented/demo range is 0..250 (percent * 2.5).
    # Mapping in level space keeps the logical ramp smooth even when the
    # physical ceiling is below 100%.
    max_level = int(round(safe_max * 2.5))
    level = int(round(logical / 100 * max_level))
    level = max(0, min(250, level))
    physical_percent = int(round(level / 2.5))
    report = bytes((_REPORT_ID, _COMMAND[0], _COMMAND[1], level, level ^ 0xFF))
    return physical_percent, level, report


class BacklightController:
    """Coalescing, reconnecting controller for one exact Waveshare HID model."""

    def __init__(
        self,
        config: Optional[Mapping] = None,
        *,
        sysfs_root: str = "/sys/class/hidraw",
        dev_root: str = "/dev",
        opener: Callable[[str, int], int] = os.open,
        writer: Callable[[int, bytes], int] = os.write,
        closer: Callable[[int], None] = os.close,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        config = config if isinstance(config, Mapping) else {}
        self.enabled = bool(config.get("enabled", True))
        configured_max = int(_bounded_number(
            config.get("safe_max_percent"),
            DEFAULT_SAFE_MAX_PERCENT,
            LOGICAL_STEP_PERCENT,
            THREE_AMP_SAFE_MAX_PERCENT,
        ))
        self.safe_max_percent = max(
            LOGICAL_STEP_PERCENT,
            (configured_max // LOGICAL_STEP_PERCENT) * LOGICAL_STEP_PERCENT,
        )
        self.initial_percent = _quantize_logical(_bounded_number(
            config.get("initial_percent"), 100, 0, 100
        ))
        self.idle_percent = _quantize_logical(_bounded_number(
            config.get("idle_percent"), 10, 0, 100
        ))
        self.ramp_interval_seconds = _bounded_number(
            config.get("ramp_interval_ms"), 150, 100, 1000
        ) / 1000
        # ``ramp_interval_ms`` historically described each ten-point band.
        # Scale the write cadence with the finer internal step so an idle/wake
        # transition keeps the same duration and electrical slew rate.
        self.ramp_write_interval_seconds = (
            self.ramp_interval_seconds
            * RAMP_STEP_PERCENT
            / LOGICAL_STEP_PERCENT
        )
        self.retry_interval_seconds = _bounded_number(
            config.get("retry_interval_seconds"), 2, 0.5, 30
        )
        # On first contact (including after re-enumeration), start at the
        # lowest non-zero logical step before ramping toward the target.
        self._first_contact_percent = LOGICAL_STEP_PERCENT
        self._contact_poll_seconds = 1.0

        self._sysfs_root = Path(sysfs_root)
        self._dev_root = Path(dev_root)
        self._opener = opener
        self._writer = writer
        self._closer = closer
        self._monotonic = monotonic

        self._condition = threading.Condition(threading.RLock())
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._desired_percent = self.initial_percent
        self._active_percent = self.initial_percent
        self._applied_percent: Optional[int] = None
        self._hardware_percent: Optional[int] = None
        self._hardware_level: Optional[int] = None
        self._mode = "active"
        self._available = False
        self._device_path: Optional[str] = None
        self._device_identity: Optional[tuple[str, int, int]] = None
        self._contact_trusted = False
        self._last_error: Optional[str] = "not_started" if self.enabled else "disabled"
        self._force_apply = self.enabled
        self._write_in_progress = False

    @classmethod
    def from_application_config(cls, config: Optional[Mapping] = None, **kwargs):
        root = config if isinstance(config, Mapping) else {}
        section = root.get("backlight") if isinstance(root.get("backlight"), Mapping) else {}
        return cls(section, **kwargs)

    def _discover_devices(self) -> list[tuple[str, tuple[str, int, int]]]:
        matches = []
        try:
            entries: Iterable[Path] = sorted(self._sysfs_root.glob("hidraw*"), key=lambda item: item.name)
        except OSError:
            entries = []
        for entry in entries:
            if not re.fullmatch(r"hidraw\d+", entry.name):
                continue
            device = entry / "device"
            matched = False
            try:
                matched = _hid_id_matches((device / "uevent").read_text(encoding="ascii"))
            except (OSError, UnicodeError):
                pass
            if not matched:
                matched = _usb_ancestor_matches(device)
            if matched:
                identity = _sysfs_contact_identity(entry)
                if identity is not None:
                    matches.append((str(self._dev_root / entry.name), identity))
        return matches

    def _discover_device_paths(self) -> list[str]:
        """Compatibility/readability wrapper used by diagnostics and tests."""
        return [path for path, _identity in self._discover_devices()]

    def _selected_device(self, devices):
        if not devices:
            return None
        with self._condition:
            cached_path = self._device_path
            cached_identity = self._device_identity
        for device in devices:
            if device == (cached_path, cached_identity):
                return device
        for device in devices:
            if device[0] == cached_path:
                return device
        return devices[0]

    def probe(self) -> bool:
        """Refresh presence without opening or writing the HID node."""
        devices = self._discover_devices() if self.enabled else []
        with self._condition:
            if not self.enabled:
                self._available = False
                self._device_path = None
                self._device_identity = None
                self._contact_trusted = False
                self._last_error = "disabled"
                return False
            if not devices:
                had_contact = self._contact_trusted or self._device_path is not None
                self._available = False
                self._device_path = None
                self._device_identity = None
                self._contact_trusted = False
                if had_contact:
                    self._force_apply = True
                if not self._write_in_progress:
                    self._last_error = "device_not_found"
                self._condition.notify_all()
                return False
            selected = self._selected_device(devices)
            path, identity = selected
            if self._contact_trusted and identity != self._device_identity:
                self._available = False
                self._device_path = path
                self._device_identity = None
                self._contact_trusted = False
                self._force_apply = True
                self._last_error = "device_contact_changed"
                self._condition.notify_all()
                return True
            self._device_path = path
            if self._last_error in ("not_started", "device_not_found"):
                self._available = True
                self._last_error = None
            elif self._last_error is None:
                self._available = True
            return True

    @staticmethod
    def _next_logical_step(current: Optional[int], target: int, first_contact: int = 10) -> int:
        if current is None:
            return min(target, first_contact) if target > first_contact else target
        if current < target:
            return min(target, current + RAMP_STEP_PERCENT)
        if current > target:
            return max(target, current - RAMP_STEP_PERCENT)
        return target

    def _write_logical_percent(
        self, logical_percent: int
    ) -> tuple[int, int, int, str, tuple[str, int, int]]:
        devices = self._discover_devices()
        with self._condition:
            trusted_identity = self._device_identity if self._contact_trusted else None
        selected = self._selected_device(devices)
        if selected is not None:
            devices.remove(selected)
            devices.insert(0, selected)
        if not devices:
            raise BacklightUnavailable("device_not_found")

        last_error = "device_unavailable"
        flags = os.O_WRONLY | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for path, identity in devices:
            fd = None
            try:
                applied_logical = logical_percent
                if trusted_identity != identity:
                    applied_logical = min(logical_percent, self._first_contact_percent)
                physical_percent, level, report = _logical_to_command(
                    applied_logical, self.safe_max_percent
                )
                fd = self._opener(path, flags)
                # Re-check after open so a basename reused between discovery
                # and write can never receive a high report as a trusted node.
                current_identity = dict(self._discover_devices()).get(path)
                if current_identity != identity:
                    last_error = "device_contact_changed"
                    continue
                written = self._writer(fd, report)
                if written != _REPORT_LENGTH:
                    raise OSError(f"short_write:{written}")
                return applied_logical, physical_percent, level, path, identity
            except PermissionError:
                last_error = "permission_denied"
            except FileNotFoundError:
                last_error = "device_disconnected"
            except OSError as error:
                errno_value = getattr(error, "errno", None)
                last_error = f"hid_write_failed:{errno_value}" if errno_value else str(error)[:80]
            finally:
                if fd is not None:
                    try:
                        self._closer(fd)
                    except OSError:
                        pass
        raise BacklightUnavailable(last_error)

    def _ensure_worker_locked(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._worker,
            name="waveshare-backlight",
            daemon=True,
        )
        self._thread.start()

    def start(self) -> None:
        """Start applying the safe initial brightness; idempotent."""
        with self._condition:
            if not self.enabled:
                return
            self._force_apply = True
            self._ensure_worker_locked()
            self._condition.notify_all()

    def stop(self, timeout: float = 1.0) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def set_percent(self, percent) -> dict:
        logical = _quantize_logical(percent)
        with self._condition:
            self._desired_percent = logical
            self._active_percent = logical
            self._mode = "active"
            self._force_apply = self._force_apply or self._applied_percent != logical
            self._ensure_worker_locked()
            self._condition.notify_all()
            return self._status_locked()

    def set_idle(self) -> dict:
        """Dim without losing the brightness selected by the user."""
        with self._condition:
            idle_target = min(self.idle_percent, self._active_percent)
            self._desired_percent = idle_target
            self._mode = "idle"
            self._force_apply = self._force_apply or self._applied_percent != idle_target
            self._ensure_worker_locked()
            self._condition.notify_all()
            return self._status_locked()

    def set_active(self) -> dict:
        """Restore the last user-selected brightness after idle."""
        with self._condition:
            self._desired_percent = self._active_percent
            self._mode = "active"
            self._force_apply = self._force_apply or self._applied_percent != self._active_percent
            self._ensure_worker_locked()
            self._condition.notify_all()
            return self._status_locked()

    def _worker(self) -> None:
        next_attempt_at = 0.0
        next_contact_check_at = self._monotonic() + self._contact_poll_seconds
        while True:
            check_contact = False
            with self._condition:
                while True:
                    if self._stop:
                        return
                    needs_apply = self._force_apply or self._applied_percent != self._desired_percent
                    now = self._monotonic()
                    if needs_apply and now >= next_attempt_at:
                        target = self._desired_percent
                        origin = self._applied_percent if self._contact_trusted else None
                        step = self._next_logical_step(origin, target, self._first_contact_percent)
                        self._write_in_progress = True
                        break
                    if now >= next_contact_check_at:
                        check_contact = True
                        break
                    deadlines = [next_contact_check_at]
                    if needs_apply:
                        deadlines.append(next_attempt_at)
                    wait_for = max(0.01, min(deadlines) - now)
                    self._condition.wait(wait_for)

            if check_contact:
                self.probe()
                next_contact_check_at = self._monotonic() + self._contact_poll_seconds
                continue

            try:
                applied_logical, physical_percent, level, path, identity = self._write_logical_percent(step)
            except BacklightUnavailable as error:
                with self._condition:
                    self._available = False
                    self._device_path = None
                    self._device_identity = None
                    self._contact_trusted = False
                    self._last_error = str(error)[:80]
                    self._force_apply = True
                    self._write_in_progress = False
                    next_attempt_at = self._monotonic() + self.retry_interval_seconds
                    next_contact_check_at = self._monotonic() + self._contact_poll_seconds
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._applied_percent = applied_logical
                    self._hardware_percent = physical_percent
                    self._hardware_level = level
                    self._device_path = path
                    self._device_identity = identity
                    self._contact_trusted = True
                    self._available = True
                    self._last_error = None
                    self._force_apply = self._applied_percent != self._desired_percent
                    self._write_in_progress = False
                    next_attempt_at = self._monotonic() + self.ramp_write_interval_seconds
                    next_contact_check_at = self._monotonic() + self._contact_poll_seconds
                    self._condition.notify_all()

    def _status_locked(self) -> dict:
        pending = self.enabled and (
            self._write_in_progress
            or self._force_apply
            or self._applied_percent != self._desired_percent
        )
        return {
            "enabled": self.enabled,
            "available": self._available,
            "mode": self._mode,
            "percent": self._desired_percent,
            "desired_percent": self._desired_percent,
            "applied_percent": self._applied_percent,
            "active_percent": self._active_percent,
            "idle_percent": self.idle_percent,
            "hardware_percent": self._hardware_percent,
            "hardware_level": self._hardware_level,
            "safe_max_percent": self.safe_max_percent,
            "step_percent": LOGICAL_STEP_PERCENT,
            "ramp_step_percent": RAMP_STEP_PERCENT,
            "pending": pending,
            "device": os.path.basename(self._device_path) if self._device_path else None,
            "error": self._last_error,
        }

    def status(self, *, refresh: bool = False) -> dict:
        if refresh:
            self.probe()
        with self._condition:
            return self._status_locked()
