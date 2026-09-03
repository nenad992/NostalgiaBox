"""HDMI-CEC input: use the TV's own remote to drive the box.

Many TVs can forward remote button presses to attached HDMI devices over CEC
(Samsung "Anynet+", LG "SimpLink", Sony "BRAVIA Sync", etc.). On a Raspberry Pi
libCEC's ``cec-client`` prints ``key pressed: up (1)`` and raw traffic such as
``>> 01:44:01`` (User Control Pressed). This backend turns those into actions.

The Pi must be the **active source** or most TVs never send keys here. After
``cec-client`` starts we send ``as`` (active source) and ``on 0`` (wake TV).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from ..actions import Action, InputEvent
from .base import InputBackend
from .keymap import cec_key_to_event

log = logging.getLogger(__name__)


def claim_kernel_cec(
    *,
    device: str = "/dev/cec0",
    osd_name: str = "NostalgiaBox",
) -> None:
    """Name the Pi on the TV and claim active source without locking /dev/cec.

    ``cec-client`` exclusive-opens the adapter (so kernel RC dies). ``cec-ctl``
    can set playback + OSD name and send ACTIVE_SOURCE, then exit.
    """
    if shutil.which("cec-ctl") is None:
        log.info("cec-ctl not found; HDMI device name stays at kernel default")
        return
    name = (osd_name or "NostalgiaBox")[:14]
    try:
        info = subprocess.run(
            ["cec-ctl", "-d", device, "--playback", f"--osd-name={name}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("cec-ctl playback setup failed: %s", exc)
        return
    if info.returncode != 0:
        log.warning("cec-ctl playback failed: %s", (info.stderr or info.stdout)[:300])
        return
    phys = _phys_addr_from_cec_ctl(info.stdout)
    if phys is None:
        phys = _phys_addr_from_cec_ctl(
            subprocess.run(
                ["cec-ctl", "-d", device],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        )
    try:
        subprocess.run(
            ["cec-ctl", "-d", device, "--to", "0", "--image-view-on"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if phys:
            subprocess.run(
                [
                    "cec-ctl",
                    "-d",
                    device,
                    "--active-source",
                    f"phys-addr={phys}",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("cec-ctl active-source failed: %s", exc)
        return
    log.info("HDMI-CEC: OSD name %r, active source %s", name, phys or "?")


def _phys_addr_from_cec_ctl(text: str) -> Optional[str]:
    match = re.search(r"Physical Address\s*:\s*([0-9a-fA-F]+\.[0-9a-fA-F]+\.[0-9a-fA-F]+\.[0-9a-fA-F]+)", text)
    return match.group(1) if match else None


# cec-client English lines (ignore "key released:").
_KEY_PRESSED_RE = re.compile(r"key pressed:\s*(.+?)\s*(?:\(|$)", re.IGNORECASE)
# User Control Pressed opcode 0x44 + operand (not 0x45 released).
_USER_CTRL_RE = re.compile(
    r">>\s*[0-9a-fA-F]{2}:44:([0-9a-fA-F]{2})\b",
)

# HDMI-CEC User Control operand -> action (CEC 1.4 table).
_CEC_OPERANDS: Dict[int, InputEvent] = {
    0x00: InputEvent(Action.ENTER),
    0x01: InputEvent(Action.CHANNEL_UP),
    0x02: InputEvent(Action.CHANNEL_DOWN),
    0x0D: InputEvent(Action.LAST_CHANNEL),
    0x30: InputEvent(Action.CHANNEL_UP),
    0x31: InputEvent(Action.CHANNEL_DOWN),
    0x32: InputEvent(Action.LAST_CHANNEL),
    0x35: InputEvent(Action.INFO),
    0x40: InputEvent(Action.POWER),
}
for _d in range(10):
    _CEC_OPERANDS[0x20 + _d] = InputEvent.digit(_d)


def parse_cec_line(line: str) -> Optional[InputEvent]:
    """Turn one cec-client log line into an InputEvent, or None."""
    if "key released" in line.lower():
        return None
    hex_match = _USER_CTRL_RE.search(line)
    if hex_match:
        operand = int(hex_match.group(1), 16)
        return _CEC_OPERANDS.get(operand)
    name_match = _KEY_PRESSED_RE.search(line)
    if name_match:
        return cec_key_to_event(name_match.group(1))
    return None


class CecBackend(InputBackend):
    """Reads TV-remote button presses forwarded over HDMI-CEC."""

    name = "cec"

    def __init__(
        self,
        *,
        binary: str = "cec-client",
        osd_name: str = "NostalgiaBox",
        extra_args: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self._binary = binary
        self._osd_name = osd_name
        self._extra_args = list(extra_args) if extra_args else []
        self._proc: Optional[subprocess.Popen] = None
        self._claimed_source = False

    @staticmethod
    def is_available(binary: str = "cec-client") -> bool:
        return shutil.which(binary) is not None

    def _run(self) -> None:
        if not self.is_available(self._binary):
            log.info("%s not found; HDMI-CEC input disabled", self._binary)
            return
        cmd = [
            self._binary,
            "-t", "p",            # register as a Playback device
            "-o", self._osd_name,
            "-d", "8",            # include key-press / traffic lines
            *self._extra_args,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log.warning("could not start %s: %s", self._binary, exc)
            return

        log.info("HDMI-CEC input active via %s", self._binary)
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self.stopping:
                break
            if not self._claimed_source:
                self._claim_active_source()
                self._claimed_source = True
            self._handle_line(line)

    def _claim_active_source(self) -> None:
        """Ask the TV to treat the Pi as the current HDMI device (sends keys here)."""
        self._send("on 0")
        self._send("as")
        log.info("HDMI-CEC: claimed active source (TV remote should target the Pi)")

    def _send(self, command: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(command.strip() + "\n")
            self._proc.stdin.flush()
        except OSError:
            log.debug("could not write CEC command %r", command, exc_info=True)

    def _handle_line(self, line: str) -> None:
        event = parse_cec_line(line)
        if event is not None:
            self.emit(event)

    def _close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            pass
        self._proc = None


__all__ = ["CecBackend", "parse_cec_line", "claim_kernel_cec"]
