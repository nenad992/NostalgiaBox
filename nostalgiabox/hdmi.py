"""HDMI connector (hotplug) presence — used to pause playback when the TV is off."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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
