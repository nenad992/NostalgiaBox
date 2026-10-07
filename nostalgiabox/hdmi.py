"""Is the TV on? Used to pause playback when the TV is off.

Two signals: the HDMI connector (hotplug) state, and the TV's HDMI-CEC power
status. Many TVs keep hotplug high in standby, so hotplug alone often never
reports "off"; CEC power status does.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_DRM = Path("/sys/class/drm")


def drm_hdmi_connected(sys_drm: Path = _DRM) -> Optional[bool]:
    """True if an HDMI connector is connected, False if all HDMI are down.

    ``None`` means we cannot tell (no DRM sysfs, or no HDMI connectors) — callers
    should treat that as "keep playing".
    """
    if not sys_drm.is_dir():
        return None
    seen = False
    any_up = False
    try:
        entries = list(sys_drm.iterdir())
    except OSError:
        return None
    for entry in entries:
        if "HDMI" not in entry.name.upper():
            continue
        status_path = entry / "status"
        try:
            status = status_path.read_text().strip().lower()
        except OSError:
            continue
        seen = True
        if status == "connected":
            any_up = True
    if not seen:
        return None
    return any_up


def hdmi_signal_present() -> Optional[bool]:
    """Whether the Pi currently has an HDMI sink. ``None`` = unknown."""
    return drm_hdmi_connected()


_PWR_RE = re.compile(r"pwr-state:\s*([a-z-]+)")


def parse_power_status(text: str) -> Optional[bool]:
    """Turn ``cec-ctl --give-device-power-status`` output into on/off/unknown."""
    match = _PWR_RE.search(text)
    if match:
        state = match.group(1)
        if state in ("on", "to-on"):
            return True
        if state in ("standby", "to-standby"):
            return False
        return None
    if "Not Acknowledged" in text:
        return False  # nothing answered on the TV's address: TV fully off
    return None


class TvPowerWatcher:
    """Polls the TV's CEC power status in the background.

    ``tv_on`` is True/False once known, None if the TV never answers. When the
    TV comes back on, ``on_wake`` runs (we use it to claim the HDMI input).
    """

    def __init__(
        self,
        *,
        device: str = "/dev/cec0",
        interval: float = 5.0,
        on_wake: Optional[Callable[[], None]] = None,
    ) -> None:
        self._device = device
        self._interval = interval
        self._on_wake = on_wake
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.tv_on: Optional[bool] = None

    @staticmethod
    def is_available(device: str = "/dev/cec0") -> bool:
        return shutil.which("cec-ctl") is not None and Path(device).exists()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tv-power", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> Optional[bool]:
        try:
            out = subprocess.run(
                ["cec-ctl", "-d", self._device, "--to", "0", "--give-device-power-status"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_power_status(out.stdout + out.stderr)

    def _run(self) -> None:
        while not self._stop.is_set():
            state = self.poll_once()
            if state is not None and state != self.tv_on:
                log.info("TV power (CEC): %s", "on" if state else "standby")
                was_off = self.tv_on is False
                self.tv_on = state
                if state and was_off and self._on_wake is not None:
                    try:
                        self._on_wake()
                    except Exception:  # noqa: BLE001
                        log.debug("on_wake failed", exc_info=True)
            self._stop.wait(self._interval)


def tv_signal(
    drm: Callable[[], Optional[bool]],
    watcher: Optional[TvPowerWatcher],
) -> Callable[[], Optional[bool]]:
    """Combine hotplug and CEC: either one saying "off" means the TV is off."""

    def present() -> Optional[bool]:
        connected = drm()
        if connected is False:
            return False
        if watcher is not None and watcher.tv_on is False:
            return False
        return connected

    return present
