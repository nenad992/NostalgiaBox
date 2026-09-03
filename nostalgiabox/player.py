"""The video player abstraction.

The application talks to an abstract :class:`Player`; two implementations exist:

* :class:`MpvPlayer` - the real thing, backed by libmpv (via the ``python-mpv``
  package). This is what runs on the Raspberry Pi against the TV.
* :class:`MockPlayer` - a no-op player that records what it was asked to do and
  lets tests/dev drive "the episode ended" by hand. This lets the entire app be
  exercised on a laptop with no display, no libmpv, and no media files.

Keeping this boundary thin (load / stop / volume / a couple of OSD hooks) means
the interesting logic in ``app.py`` never has to know which one it is using.
"""

from __future__ import annotations

import logging
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

def should_emit_eof(*, suppress: bool, ignore_next_eof: bool) -> tuple[bool, bool]:
    """Whether a natural EOF should roll the channel, and the next ignore flag.

    Channel-change static is a playlist item ahead of the episode; its EOF must
    not advance the channel. The following EOF (the episode) should.
    """
    if suppress:
        return False, ignore_next_eof
    if ignore_next_eof:
        return False, False
    return True, False
END_EOF = "eof"        # the file played to its natural end -> roll next episode
END_ERROR = "error"    # the file failed to play -> skip to next episode
END_STOPPED = "stopped"  # we stopped it on purpose (channel change) -> ignore


class Player(ABC):
    """Minimal video-player interface used by the application."""

    #: Called when playback of the current item finishes. Receives one of the
    #: END_* reason strings. Set by the application before playing anything.
    on_end: Optional[Callable[[str], None]] = None

    @abstractmethod
    def play(self, path: Path, *, start: float = 0.0) -> None:
        """Begin playing ``path`` from ``start`` seconds in."""

    @abstractmethod
    def play_loop(self, path: Path) -> None:
        """Play ``path`` on an endless loop (used for the static/no-signal clip)."""

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        """Show a brief static burst, then the target episode.

        The default implementation just plays the target; players that can
        preload (see :class:`MpvPlayer`) override this to make the switch
        near-instant.
        """
        self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        """Begin loading ``target_path`` in the background while the CURRENT item
        keeps playing. Call :meth:`commit_switch` to cut over once it's ready.

        The default implementation has no way to preload, so it just plays the
        target immediately; :class:`MpvPlayer` overrides it.
        """
        self.play(target_path, start=start)

    def commit_switch(self) -> None:
        """Switch to the item queued by :meth:`preload_next` (no-op by default)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop playback and show a blank screen."""

    @abstractmethod
    def set_volume(self, volume: int) -> None:
        """Set the volume (0-100)."""

    @abstractmethod
    def set_mute(self, muted: bool) -> None: ...

    @abstractmethod
    def get_time_pos(self) -> Optional[float]:
        """Current playback position in seconds, or None if nothing is playing."""

    @abstractmethod
    def show_text(self, text: str, duration: float) -> None:
        """Show a plain OSD message for ``duration`` seconds."""

    @abstractmethod
    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        """Draw an ASS overlay with the given id (replacing any previous one)."""

    @abstractmethod
    def clear_overlay(self, overlay_id: int) -> None:
        """Remove a previously drawn overlay."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""

    def pump_events(self, timeout: float = 0.02) -> None:
        """Service OS window events. No-op except where the VO needs it (macOS)."""


def use_drm_gpu_output(
    *,
    platform: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """True on a headless Linux console (Pi KMS), not under X11/Wayland."""
    plat = sys.platform if platform is None else platform
    environ = os.environ if env is None else env
    if not plat.startswith("linux"):
        return False
    if environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
        return False
    return True


def mpv_player_options(
    *,
    fullscreen: bool = True,
    hwdec: str = "auto-safe",
    glsl_shaders: Optional[str] = None,
    force_4_3: bool = False,
    audio_device: Optional[str] = None,
    extra_options: Optional[dict] = None,
) -> dict:
    """libmpv constructor kwargs.

    ``force_window=yes`` opens the picture window once something is playing.
    python-mpv on this libmpv rejects ``immediate`` at init (error -4), so we
    keep a file playing (colour bars on empty channels) instead.
    """
    options: dict = dict(
        osc=False,
        input_default_bindings=False,
        input_vo_keyboard=False,
        idle="yes",
        force_window="yes",
        keep_open="yes",
        prefetch_playlist="yes",
        fullscreen=fullscreen,
        hwdec=hwdec,
        keepaspect="yes",
        video_unscaled="no",
        panscan=1.0,
        cursor_autohide="always",
        osd_font_size=40,
    )
    # Default vo=drm on a Pi console cannot run GLSL user shaders (no CRT).
    if use_drm_gpu_output():
        options["vo"] = "gpu"
        options["gpu_context"] = "drm"
    if not fullscreen:
        options["geometry"] = "1280x720"
        options["title"] = "NostalgiaBox"
    if audio_device:
        options["audio_device"] = audio_device
    if glsl_shaders:
        options["glsl_shaders"] = glsl_shaders
    if force_4_3:
        options.pop("panscan", None)
        options["vf"] = (
            "lavfi=[scale=960:720:force_original_aspect_ratio=decrease,"
            "pad=960:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1]"
        )
    if extra_options:
        options.update(extra_options)
    return options


class MpvPlayer(Player):
    """A :class:`Player` backed by libmpv, tuned for a Raspberry Pi + TV."""

    def __init__(
        self,
        *,
        fullscreen: bool = True,
        hwdec: str = "auto-safe",
        glsl_shaders: Optional[str] = None,
        fonts_dir: Optional[Path] = None,
        force_4_3: bool = False,
        audio_device: Optional[str] = None,
        extra_options: Optional[dict] = None,
    ) -> None:
        try:
            import mpv  # type: ignore
        except ImportError as exc:  # pragma: no cover - only on machines w/o libmpv
            raise RuntimeError(
                "python-mpv/libmpv is not installed. On the Raspberry Pi run "
                "`scripts/install.sh` or `pip install .[pi]` and ensure libmpv "
                "is present (`sudo apt install libmpv2 mpv`)."
            ) from exc

        # Make our bundled retro font discoverable by libass (used for the OSD
        # overlays) by dropping it into mpv's config "fonts" directory.
        if fonts_dir is not None:
            _install_fonts_for_mpv(fonts_dir)

        self._mpv = mpv.MPV(
            **mpv_player_options(
                fullscreen=fullscreen,
                hwdec=hwdec,
                glsl_shaders=glsl_shaders,
                force_4_3=force_4_3,
                audio_device=audio_device,
                extra_options=extra_options,
            )
        )
        if sys.platform == "darwin":
            from .macos_cocoa import ensure_nsapplication

            ensure_nsapplication()
        self._closed = False
        self._suppress = True
        self._ignore_next_eof = False

        @self._mpv.property_observer("eof-reached")
        def _on_eof(_name, value):  # pragma: no cover - needs libmpv + media
            if value:
                emit, self._ignore_next_eof = should_emit_eof(
                    suppress=self._suppress, ignore_next_eof=self._ignore_next_eof
                )
                if emit and self.on_end is not None:
                    try:
                        self.on_end(END_EOF)
                    except Exception:  # noqa: BLE001 - never let a callback kill mpv
                        log.exception("error in on_end (eof) callback")

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):  # pragma: no cover - needs libmpv + media
            # We only care about *errors* here (e.g. a corrupt/missing file) so
            # we can skip to the next episode. Natural ends are handled by the
            # eof-reached observer above; intentional stops/replacements are
            # ignored.
            if self._suppress:
                return
            if _extract_end_reason(event) == END_ERROR and self.on_end is not None:
                try:
                    self.on_end(END_ERROR)
                except Exception:  # noqa: BLE001
                    log.exception("error in on_end (error) callback")

    # -- playback -----------------------------------------------------------
    def play(self, path: Path, *, start: float = 0.0) -> None:
        # Enable end detection only for real content.
        self._suppress = False
        self._ignore_next_eof = False
        try:
            self._mpv.loop_file = "no"
            if start and start > 0:
                # start is an mpv per-file option; +N seeks N seconds in.
                self._mpv.loadfile(str(path), "replace", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False  # keep-open can leave us paused; force play
        except Exception:  # noqa: BLE001
            log.exception("failed to play %s", path)
            if self.on_end is not None:
                self.on_end(END_ERROR)

    def play_loop(self, path: Path) -> None:
        self._suppress = True  # a looping clip should never trigger "next"
        try:
            self._mpv.loop_file = "inf"
            self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed to loop %s", path)

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        # Playlist: [static burst, episode]. Ignore the static clip's EOF so we
        # do not skip the episode we just tuned.
        self._suppress = False
        self._ignore_next_eof = True
        try:
            self._mpv.loop_file = "no"
            self._mpv.loadfile(
                str(static_path), "replace", end=f"{max(0.05, static_seconds):.3f}"
            )
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed transition to %s", target_path)
            self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # Keep the currently-playing item on screen and append the target as a
        # second playlist entry. With prefetch-playlist=yes, mpv opens/decodes it
        # in the background while the current show keeps playing, so commit_switch
        # can cut over near-instantly (no frozen frame).
        self._suppress = True  # ignore the outgoing show's own eof during the bridge
        try:
            self._mpv.command("playlist-clear")  # drop any earlier pending append
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
        except Exception:  # noqa: BLE001
            log.exception("failed to preload %s", target_path)
            self.play(target_path, start=start)

    def commit_switch(self) -> None:
        self._suppress = False
        try:
            self._mpv.command("playlist-next", "force")  # jump to the prefetched item
            self._mpv.command("playlist-clear")          # keep only the new current
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.debug("commit_switch failed", exc_info=True)

    def stop(self) -> None:
        self._suppress = True
        try:
            self._mpv.command("stop")
        except Exception:  # noqa: BLE001 - stopping should never crash us
            log.debug("mpv stop failed", exc_info=True)

    # -- audio --------------------------------------------------------------
    def set_volume(self, volume: int) -> None:
        try:
            self._mpv.volume = max(0, min(100, int(volume)))
        except Exception:  # noqa: BLE001
            log.debug("could not set volume", exc_info=True)

    def set_mute(self, muted: bool) -> None:
        try:
            self._mpv.mute = bool(muted)
        except Exception:  # noqa: BLE001
            log.debug("could not set mute", exc_info=True)

    def get_time_pos(self) -> Optional[float]:
        try:
            pos = self._mpv.time_pos
            return float(pos) if pos is not None else None
        except Exception:  # noqa: BLE001
            return None

    # -- OSD ----------------------------------------------------------------
    def show_text(self, text: str, duration: float) -> None:
        try:
            self._mpv.command("show-text", text, int(duration * 1000))
        except Exception:  # noqa: BLE001
            log.debug("show-text failed", exc_info=True)

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        try:
            # osd-overlay positional args: id, format, data, res_x, res_y.
            # (Trailing z/hidden/compute_bounds use their defaults.)
            self._mpv.command(
                "osd-overlay", overlay_id, "ass-events", ass, res_x, res_y
            )
        except Exception:  # noqa: BLE001
            # Fall back to a plain message so the viewer still gets feedback.
            log.debug("osd-overlay failed, falling back to show-text", exc_info=True)
            self.show_text(_strip_ass(ass), 3.0)

    def clear_overlay(self, overlay_id: int) -> None:
        try:
            self._mpv.command("osd-overlay", overlay_id, "none", "")
        except Exception:  # noqa: BLE001
            log.debug("clearing overlay failed", exc_info=True)

    def pump_events(self, timeout: float = 0.02) -> None:
        if sys.platform != "darwin":
            return
        from .macos_cocoa import pump

        pump(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._mpv.terminate()
        except Exception:  # noqa: BLE001
            log.debug("mpv terminate failed", exc_info=True)


class MockPlayer(Player):
    """A headless stand-in that records commands - for tests and dev mode."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.current: Optional[Path] = None
        self.looping: Optional[Path] = None
        self.volume: int = 0
        self.muted: bool = False
        self.time_pos: float = 0.0
        self.closed = False
        # Recorded history, handy for assertions in tests.
        self.played: List[Tuple[Path, float]] = []
        self.transitions: List[Tuple[Path, Path, float]] = []
        self.preloaded: Optional[Tuple[Path, float]] = None
        self.messages: List[Tuple[str, float]] = []
        self.overlays: dict[int, str] = {}
        self.stops = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[player] {msg}")

    def play(self, path: Path, *, start: float = 0.0) -> None:
        self.current = path
        self.looping = None
        self.time_pos = start
        self.played.append((path, start))
        self._log(f"PLAY {path} @ {start:.1f}s")

    def play_loop(self, path: Path) -> None:
        self.looping = path
        self.current = path
        self._log(f"LOOP {path}")

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        self.transitions.append((static_path, target_path, start))
        # The episode is what ends up playing (static is momentary).
        self.current = target_path
        self.looping = None
        self.time_pos = start
        self.played.append((target_path, start))
        self._log(f"TRANSITION static={static_path} -> {target_path} @ {start:.1f}s")

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # The current item keeps "playing"; the target is queued, not shown yet.
        self.preloaded = (target_path, start)
        self._log(f"PRELOAD {target_path} @ {start:.1f}s (current keeps playing)")

    def commit_switch(self) -> None:
        if self.preloaded is None:
            return
        target, start = self.preloaded
        self.preloaded = None
        self.current = target
        self.looping = None
        self.time_pos = start
        self.played.append((target, start))
        self._log(f"COMMIT SWITCH -> {target} @ {start:.1f}s")

    def stop(self) -> None:
        self.current = None
        self.looping = None
        self.preloaded = None
        self.stops += 1
        self._log("STOP")

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._log(f"VOLUME {self.volume}")

    def set_mute(self, muted: bool) -> None:
        self.muted = bool(muted)
        self._log(f"MUTE {self.muted}")

    def get_time_pos(self) -> Optional[float]:
        return self.time_pos if self.current is not None else None

    def show_text(self, text: str, duration: float) -> None:
        self.messages.append((text, duration))
        self._log(f"TEXT {text!r} ({duration}s)")

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        self.overlays[overlay_id] = ass
        self._log(f"OVERLAY {overlay_id}")

    def clear_overlay(self, overlay_id: int) -> None:
        self.overlays.pop(overlay_id, None)
        self._log(f"CLEAR OVERLAY {overlay_id}")

    def close(self) -> None:
        self.closed = True
        self._log("CLOSE")

    # -- test/dev helper ----------------------------------------------------
    def finish_current(self, reason: str = END_EOF) -> None:
        """Simulate the current episode ending, triggering ``on_end``."""
        self.current = None
        if self.on_end is not None:
            self.on_end(reason)


def _extract_end_reason(event) -> str:  # pragma: no cover - libmpv specific
    """Normalise the many shapes of a python-mpv end-file event into a reason."""
    reason = None
    try:
        data = getattr(event, "data", event)
        if isinstance(data, dict):
            reason = data.get("reason")
        else:
            reason = getattr(data, "reason", None)
    except Exception:  # noqa: BLE001
        reason = None
    reason = str(reason).lower() if reason is not None else ""
    if "eof" in reason:
        return END_EOF
    if "error" in reason:
        return END_ERROR
    if "stop" in reason or "quit" in reason:
        return END_STOPPED
    # Unknown/redirect reasons: treat as a natural end so the channel keeps going.
    return END_EOF


def _install_fonts_for_mpv(fonts_dir: Path) -> None:
    """Copy bundled .ttf fonts into mpv's config 'fonts' dir so libass finds them.

    mpv automatically loads any fonts placed in ``<mpv config dir>/fonts``, which
    is the most reliable way to make our retro OSD font available to the ASS
    overlays without touching the system-wide fontconfig setup.
    """
    import os
    import shutil

    if not fonts_dir.is_dir():
        return
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dest = Path(config_home) / "mpv" / "fonts"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for ttf in fonts_dir.glob("*.ttf"):
            target = dest / ttf.name
            if not target.exists():
                shutil.copy2(ttf, target)
    except OSError:
        log.debug("could not install bundled fonts for mpv", exc_info=True)


def _strip_ass(ass: str) -> str:  # pragma: no cover - trivial
    """Very small ASS-tag stripper for the show-text fallback path."""
    import re

    text = re.sub(r"\{[^}]*\}", "", ass)
    text = text.replace("\\N", " ").replace("\\n", " ")
    return text.strip()


__all__ = [
    "mpv_player_options",
    "Player",
    "MpvPlayer",
    "MockPlayer",
    "should_emit_eof",
    "END_EOF",
    "END_ERROR",
    "END_STOPPED",
]
