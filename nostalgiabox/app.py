"""The television itself: the state machine that ties everything together.

:class:`TVApp` owns the channel lineup, the player, the overlays and the input
queue, and turns remote-control actions into TV behaviour: changing channels
(with a burst of static and a channel banner), adjusting and muting the volume,
direct channel entry by number, an info banner, a "last channel" jump, and a
standby/off mode. When an episode ends it automatically rolls into the next one
on that channel's shuffle, so the box never stops "broadcasting".

The class is written to be testable without a display: pass it a
:class:`~nostalgiabox.player.MockPlayer` and a fake clock and you can single-step
the whole thing (see ``step`` / ``handle_event`` / ``process_pending``).
"""

from __future__ import annotations

import logging
import queue
import signal
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .actions import Action, InputEvent
from .channel import Channel, ChannelLineup, PlayRequest, build_lineup
from .config import Config
from .hdmi import hdmi_signal_present
from .input.manager import InputManager, create_backends
from .overlay import OverlayManager
from .player import END_EOF, END_ERROR, MockPlayer, Player
from .static_gen import (
    COLORBARS_FILENAME,
    DEFAULT_ASSETS_DIR,
    GLITCH_FILENAME,
    STATIC_FILENAME,
)

log = logging.getLogger(__name__)


class TVApp:
    """The retro-TV application state machine."""

    def __init__(
        self,
        config: Config,
        player: Player,
        input_manager: InputManager,
        *,
        overlay: Optional[OverlayManager] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = datetime.now,
        assets_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.player = player
        self.input = input_manager
        self.overlay = overlay or OverlayManager(player, config, clock=clock)
        self._clock = clock
        self._wall_clock = wall_clock
        self._last_nightly = wall_clock()

        self.lineup: ChannelLineup = build_lineup(config)

        # Runtime state.
        self.volume = config.initial_volume
        self.muted = False
        self.standby = False
        self.powered_off = False
        self.hdmi_idle = False
        self._hdmi_lost_at: Optional[float] = None
        self._hdmi_signal = hdmi_signal_present
        self._media_present = self._library_present()
        self._playing_path: Optional[Path] = None
        self._last_channel_number: Optional[int] = None
        self._running = False

        # Direct channel entry ("type 1 then 2 -> channel 12").
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self._digit_entry_timeout = 2.0

        # Pending "bridge" switch: keep the old show playing until this deadline,
        # then cut to the channel that was preloaded. The channel banner is shown
        # at the moment of the cut-over, not when the button is pressed.
        self._switch_deadline: Optional[float] = None
        self._pending_banner: Optional[tuple[int, str]] = None
        self._pending_request: Optional[PlayRequest] = None

        # Playback-finished events from the player (may arrive on any thread).
        self._ended: "queue.Queue[str]" = queue.Queue()
        self.player.on_end = self._ended.put

        # Filler assets.
        self._assets_dir = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        self._colorbars_path = self._resolve_asset(COLORBARS_FILENAME)
        # The channel-change transition clip depends on the configured effect.
        self._transition_path = self._resolve_transition_asset()

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        player: Optional[Player] = None,
        input_manager: Optional[InputManager] = None,
        dry_run: bool = False,
        assets_dir: Optional[Path] = None,
    ) -> "TVApp":
        """Build a fully wired app, creating real hardware backends by default.

        ``dry_run`` swaps in a :class:`MockPlayer` and disables all real input
        backends (a stdin backend is added if a TTY is available), which is how
        the box can be exercised on a development machine.
        """
        if player is None:
            if dry_run:
                player = MockPlayer(verbose=True)
            else:
                from .crt import write_shader
                from .player import MpvPlayer, use_drm_gpu_output

                assets = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
                crt = config.crt
                hwdec = "auto-safe"
                # Pi 4: auto-safe tries CUDA; GLSL scanlines on 1080p stall the GPU.
                if use_drm_gpu_output():
                    crt = replace(crt, scanlines=False)
                    hwdec = "v4l2m2m"
                shader_path = write_shader(crt)
                player = MpvPlayer(
                    fullscreen=config.fullscreen,
                    hwdec=hwdec,
                    glsl_shaders=str(shader_path) if shader_path else None,
                    fonts_dir=assets / "fonts",
                    force_4_3=config.force_4_3,
                    audio_device=config.audio_device,
                )

        if input_manager is None:
            if dry_run:
                backends = create_backends({"keyboard": False, "cec": False, "stdin": True})
            else:
                backends = create_backends(config.input_options)
            input_manager = InputManager(backends)

        return cls(config, player, input_manager, assets_dir=assets_dir)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Power on: set volume, start input, and tune to the first channel."""
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        self.input.start()
        self._select_start_channel()
        self.tune_current(show_static=False)

    def run(self) -> None:
        """Run the blocking main loop until a QUIT action is received."""
        self.start()
        self._running = True
        log.info("NostalgiaBox is on the air. %d channels.", len(self.lineup))
        def _stop(_signum=None, _frame=None) -> None:
            self._running = False

        prev_term = signal.signal(signal.SIGTERM, _stop)
        try:
            while self._running:
                self.step(block=True)
        except KeyboardInterrupt:  # pragma: no cover - interactive convenience
            log.info("interrupted; shutting down")
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        try:
            self.overlay.clear_all()
        except Exception:  # noqa: BLE001
            pass
        self.input.stop()
        self.player.close()

    # -- main-loop step (small and testable) --------------------------------
    def step(self, *, block: bool = False, timeout: float = 0.1) -> None:
        """Advance the state machine by one iteration.

        Handles overlay expiry, channel-entry timeouts, finished episodes, and
        at most one queued input event.
        """
        now = self._clock()
        self.overlay.tick()
        self._tick_library()
        self._tick_nightly_rescan()
        self._tick_hdmi_idle(now)
        self._maybe_commit_switch(now)
        self._maybe_commit_digits(now)
        self._drain_playback_events()

        if block:
            event = None
            remaining = timeout
            while event is None and remaining > 0:
                # libmpv on macOS only maps its window if Cocoa is pumped on
                # the main thread; InputManager.get() would otherwise starve it.
                self.player.pump_events(min(0.02, remaining))
                slice_timeout = min(0.02, remaining)
                event = self.input.get(timeout=slice_timeout)
                remaining -= slice_timeout
        else:
            self.player.pump_events(0.0)
            event = self.input.get(timeout=0.0)
        if event is not None:
            self.handle_event(event)

    def _maybe_commit_switch(self, now: float) -> None:
        """Cut over to the preloaded channel once the bridge window has elapsed."""
        if self._switch_deadline is not None and now >= self._switch_deadline:
            self._switch_deadline = None
            self.player.commit_switch()
            # Flash the channel banner right as the picture actually changes.
            if self._pending_banner is not None:
                self.overlay.show_channel_bug(*self._pending_banner)
                if self._pending_request is not None:
                    self._flash_guide(self.lineup.current, self._pending_request)
                self._pending_banner = None
                self._pending_request = None

    # -- input handling -----------------------------------------------------
    def handle_event(self, event: InputEvent) -> None:
        action = event.action

        if action == Action.QUIT:
            self._running = False
            return
        if action == Action.POWER:
            self._toggle_standby()
            return

        # While in standby, ignore everything except POWER/QUIT (handled above).
        if self.standby:
            return

        handlers = {
            Action.CHANNEL_UP: self._channel_up,
            Action.CHANNEL_DOWN: self._channel_down,
            Action.VOLUME_UP: self._volume_up,
            Action.VOLUME_DOWN: self._volume_down,
            Action.MUTE: self._toggle_mute,
            Action.INFO: self._show_info,
            Action.LAST_CHANNEL: self._jump_last_channel,
            Action.ENTER: self._confirm_digits,
        }
        if action == Action.DIGIT:
            self._push_digit(event.value or 0)
        else:
            handler = handlers.get(action)
            if handler is not None:
                handler()

    # -- channel changing ---------------------------------------------------
    def _channel_up(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.up()
        self.tune_current()

    def _channel_down(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.down()
        self.tune_current()

    def _jump_last_channel(self) -> None:
        if self._last_channel_number is None:
            return
        target = self._last_channel_number
        if not self.lineup.has_number(target):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(target)
        self.tune_current()

    def select_channel_number(self, number: int) -> bool:
        """Tune directly to a channel number. Returns False if it doesn't exist."""
        if not self.lineup.has_number(number):
            self.overlay.show_message(f"CH {number:02d}  -  NO CHANNEL")
            return False
        if number == self.lineup.current.number:
            self._show_info()
            return True
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(number)
        self.tune_current()
        return True

    def tune_current(self, *, show_static: bool = True) -> None:
        """Tune into the currently selected channel."""
        if self.hdmi_idle:
            return
        channel = self.lineup.current
        self.overlay.clear_standby()
        self.overlay.clear_message()

        request = channel.tune_in()
        self._pending_banner = None
        self._pending_request = None

        if request is None:
            # No episodes on this channel: show the "no signal" screen.
            self.overlay.show_channel_bug(channel.number, channel.name)
            self.overlay.clear_guide()
            self._show_no_signal(channel)
            return

        if not show_static:
            # Not a channel change (first tune / waking from standby): play now.
            self._switch_deadline = None
            self._flash_tune_osd(channel, request)
            self._play_request(request)
        elif self._transition_path is not None:
            # Transition clip (glitch/static) + preloaded episode.
            self._switch_deadline = None
            self._flash_tune_osd(channel, request)
            self._playing_path = request.path
            self.player.play_transition(
                self._transition_path,
                request.path,
                start=request.start,
                static_seconds=self.config.transition_duration,
            )
        elif self.config.bridge_seconds > 0 and self._playing_path is not None:
            # No transition effect: keep the current show playing while the next
            # channel preloads, then cut over (no frozen frame). The banner is
            # shown at the cut-over (see _maybe_commit_switch), not right now.
            self._playing_path = request.path
            self.player.preload_next(request.path, start=request.start)
            self._switch_deadline = self._clock() + self.config.bridge_seconds
            self._pending_banner = (channel.number, channel.name)
            self._pending_request = request
        else:
            self._switch_deadline = None
            self._flash_tune_osd(channel, request)
            self._play_request(request)

    def _play_request(self, request: PlayRequest) -> None:
        self._playing_path = request.path
        self.player.play(request.path, start=request.start)

    def _flash_tune_osd(self, channel: Channel, request: PlayRequest) -> None:
        self.overlay.show_channel_bug(channel.number, channel.name)
        self._flash_guide(channel, request)

    def _flash_guide(self, channel: Channel, request: PlayRequest) -> None:
        now_name, next_name = channel.guide_filenames(request.path)
        self.overlay.show_guide(now_name, next_name)

    def _show_no_signal(self, channel: Channel) -> None:
        self._switch_deadline = None
        self._pending_banner = None
        self._pending_request = None
        self._playing_path = None
        if self._colorbars_path is not None:
            self.player.play_loop(self._colorbars_path)
        else:
            self.player.stop()
        self.overlay.show_message(self.config.empty_channel_message, duration=0)

    # -- volume -------------------------------------------------------------
    def _volume_up(self) -> None:
        self._set_volume(self.volume + self.config.volume_step, unmute=True)

    def _volume_down(self) -> None:
        self._set_volume(self.volume - self.config.volume_step, unmute=True)

    def _set_volume(self, value: int, *, unmute: bool = False) -> None:
        self.volume = max(0, min(100, value))
        if unmute and self.muted:
            self.muted = False
            self.player.set_mute(False)
        self.player.set_volume(self.volume)
        self.overlay.show_volume(self.volume, self.muted)

    def _power_off(self) -> None:
        """Cleanly shut the Pi down so it's safe to unplug."""
        log.info("powering off (volume floor)")
        self.powered_off = True
        self._switch_deadline = None
        self._pending_banner = None
        self._pending_request = None
        try:
            self.overlay.clear_all()
            self.overlay.show_message("GOODBYE", duration=0)
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._run_power_off_command()
        self._running = False  # exit the main loop

    def _run_power_off_command(self) -> None:
        command = list(self.config.power_off_command)
        if not command:
            return  # disabled / test mode
        try:
            subprocess.Popen(command)
        except Exception:  # noqa: BLE001
            log.exception("power-off command failed: %s", command)

    def _toggle_mute(self) -> None:
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.overlay.show_volume(self.volume, self.muted)

    # -- info / standby -----------------------------------------------------
    def _show_info(self) -> None:
        channel = self.lineup.current
        self.overlay.show_channel_bug(channel.number, channel.name)
        if self._playing_path is not None:
            now_name, next_name = channel.guide_filenames(self._playing_path)
            self.overlay.show_guide(now_name, next_name)

    def _toggle_standby(self) -> None:
        self.standby = not self.standby
        if self.standby:
            self._remember_position()
            self._switch_deadline = None
            self._pending_banner = None
            self._pending_request = None
            self.player.stop()
            self.overlay.clear_all()
            self.overlay.show_standby()
        else:
            self.overlay.clear_standby()
            self.tune_current(show_static=False)

    def _tick_hdmi_idle(self, now: float) -> None:
        """Stop decoding after HDMI has been down; retune when it returns."""
        seconds = self.config.hdmi_idle_pause_seconds
        if seconds <= 0:
            if self.hdmi_idle:
                self._wake_from_hdmi_idle()
            self._hdmi_lost_at = None
            return
        present = self._hdmi_signal()
        connected = True if present is None else bool(present)
        if connected:
            self._hdmi_lost_at = None
            if self.hdmi_idle:
                self._wake_from_hdmi_idle()
            return
        if self.standby or self.hdmi_idle:
            return
        if self._hdmi_lost_at is None:
            self._hdmi_lost_at = now
            return
        if now - self._hdmi_lost_at >= seconds:
            self._enter_hdmi_idle()

    def _enter_hdmi_idle(self) -> None:
        self.hdmi_idle = True
        self._switch_deadline = None
        self._pending_banner = None
        self._pending_request = None
        self._playing_path = None
        self.player.stop()
        self.overlay.clear_all()
        log.info("HDMI idle: playback stopped (broadcast clock still running)")

    def _wake_from_hdmi_idle(self) -> None:
        self.hdmi_idle = False
        self._hdmi_lost_at = None
        if self.standby:
            return
        log.info("HDMI back: tuning to current channel")
        self.tune_current(show_static=False)

    # -- direct channel entry ----------------------------------------------
    def _push_digit(self, digit: int) -> None:
        self._digit_buffer = (self._digit_buffer + str(digit))[-3:]
        self._digit_deadline = self._clock() + self._digit_entry_timeout
        self.overlay.show_message(f"CH {self._digit_buffer}_", duration=self._digit_entry_timeout)

    def _confirm_digits(self) -> None:
        if not self._digit_buffer:
            return
        number = int(self._digit_buffer)
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self.select_channel_number(number)

    def _maybe_commit_digits(self, now: float) -> None:
        if self._digit_buffer and now >= self._digit_deadline:
            self._confirm_digits()

    # -- playback-finished handling ----------------------------------------
    def _drain_playback_events(self) -> None:
        advanced = False
        while True:
            try:
                reason = self._ended.get_nowait()
            except queue.Empty:
                break
            # Coalesce: only advance once even if several events queued up.
            if reason in (END_EOF, END_ERROR) and not advanced and not self.standby and not self.hdmi_idle:
                self._advance_current()
                advanced = True

    def _advance_current(self) -> None:
        channel = self.lineup.current
        current = self._playing_path
        if current is not None and channel.ends_cycle(current):
            self._refresh_library()
            channel = self.lineup.current
        if current is not None:
            request = channel.play_after(current)
        else:
            request = channel.advance()
        if request is None:
            self._show_no_signal(self.lineup.current)
        else:
            self._play_request(request)

    def _library_present(self) -> bool:
        if self.config.mixed is not None:
            roots = [self.config.mixed.path]
        else:
            roots = [c.path for c in self.config.channels]
        if not roots:
            return True
        try:
            return any(p.is_dir() for p in roots)
        except OSError:
            return False

    def _resume_snapshot(self) -> dict[int, tuple[Path, float]]:
        out: dict[int, tuple[Path, float]] = {}
        for channel in self.lineup:
            if channel._resume_path is not None:
                out[channel.number] = (channel._resume_path, channel._resume_position)
        return out

    def _restore_resume(self, snapshot: dict[int, tuple[Path, float]]) -> None:
        for channel in self.lineup:
            saved = snapshot.get(channel.number)
            if saved is not None:
                channel.remember(*saved)

    def _refresh_library(self, *, keep_playback: bool = False) -> None:
        current_number = self.lineup.current.number
        playing = self._playing_path
        resume = self._resume_snapshot()
        self.lineup = build_lineup(self.config)
        if self.lineup.has_number(current_number):
            self.lineup.select_number(current_number)
        self._restore_resume(resume)
        log.info("library rescanned; %d channels", len(self.lineup))
        if keep_playback and playing is not None:
            try:
                still = playing.resolve() in {
                    p.resolve() for p in self.lineup.current.episodes
                }
            except OSError:
                still = playing in self.lineup.current.episodes
            if still:
                return
            if not self.standby and not self.hdmi_idle:
                self.tune_current(show_static=False)

    def _tick_nightly_rescan(self) -> None:
        hour = self.config.library_rescan_hour
        if hour < 0:
            return
        now = self._wall_clock()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now < target:
            return
        if self._last_nightly >= target:
            return
        if self._hdmi_is_live():
            log.info("nightly library rescan skipped; HDMI is playing")
            self._last_nightly = now
            return
        log.info("nightly library rescan (%02d:00)", hour)
        self._last_nightly = now
        self._refresh_library(keep_playback=True)

    def _hdmi_is_live(self) -> bool:
        """True only when the Pi sees a live HDMI sink and we are outputting picture."""
        if self.standby or self.hdmi_idle:
            return False
        present = self._hdmi_signal()
        if present is not True:
            return False
        return self._playing_path is not None

    def _tick_library(self) -> None:
        present = self._library_present()
        if present == self._media_present:
            return
        self._media_present = present
        if present:
            log.info("media folder is back; remapping library")
        else:
            log.info("media folder missing (USB unplugged?)")
        self._refresh_library()
        if not self.standby and not self.hdmi_idle:
            self.tune_current(show_static=False)

    # -- helpers ------------------------------------------------------------
    def _remember_position(self) -> None:
        if self.config.tune_in != "resume" or self._playing_path is None:
            return
        pos = self.player.get_time_pos()
        if pos is not None:
            self.lineup.current.remember(self._playing_path, pos)

    def _select_start_channel(self) -> None:
        if self.config.start_channel is not None and self.lineup.has_number(
            self.config.start_channel
        ):
            self.lineup.select_number(self.config.start_channel)

    def _resolve_asset(self, filename: str) -> Optional[Path]:
        path = self._assets_dir / filename
        return path if path.is_file() else None

    def _resolve_transition_asset(self) -> Optional[Path]:
        effect = self.config.transition_effect
        if effect == "none":
            return None
        filename = GLITCH_FILENAME if effect == "glitch" else STATIC_FILENAME
        return self._resolve_asset(filename)


def run_from_config(config: Config, *, dry_run: bool = False) -> None:
    """Convenience entry point used by the CLI."""
    app = TVApp.from_config(config, dry_run=dry_run)
    app.run()


__all__ = ["TVApp", "run_from_config"]
