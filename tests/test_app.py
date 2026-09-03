import pytest
from datetime import datetime

from nostalgiabox.actions import Action, InputEvent
from nostalgiabox.app import TVApp
from nostalgiabox.config import config_from_dict
from nostalgiabox.input.manager import InputManager
from nostalgiabox.player import END_EOF, MockPlayer
from tests.helpers import FakeClock, make_show


def build_app(tmp_path, *, assets_dir=None, **overrides):
    for name in ("dragon", "arthur", "rugrats"):
        make_show(tmp_path, name, 4)
    data = {
        "shuffle_seed": 7,
        "start_channel": 2,
        "start_offset": 0,  # keep test assertions on start=0 unless overridden
        "power_off_command": [],  # no-op in tests (never actually shut down)
        "channels": [
            {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
            {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            {"number": 4, "name": "Rugrats", "path": str(tmp_path / "rugrats")},
        ],
    }
    data.update(overrides)
    config = config_from_dict(data)
    clock = FakeClock()
    player = MockPlayer()
    app = TVApp(
        config,
        player,
        InputManager([]),
        clock=clock,
        assets_dir=assets_dir,
    )
    return app, player, clock


def send(app, action, value=None):
    app.handle_event(InputEvent(action, value))


def test_start_tunes_to_start_channel_and_plays(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.lineup.current.number == 2
    assert player.current is not None  # an episode is playing
    assert player.volume == 100
    assert player.overlays.get(1) and "Dragon Tales" in player.overlays[1]
    assert player.overlays.get(5) and "NOW" in player.overlays[5] and "NEXT" in player.overlays[5]
    assert player.current is not None
    assert f"NOW  {player.current.stem}" in player.overlays[5]


def test_channel_change_now_next_survives_fast_zaps(tmp_path):
    app, player, clock = build_app(tmp_path, bridge_seconds=0)
    app.start()
    send(app, Action.CHANNEL_UP)
    send(app, Action.CHANNEL_UP)
    assert "NOW" in player.overlays[5] and "NEXT" in player.overlays[5]
    clock.advance(5.1)
    app.overlay.tick()
    assert 5 not in player.overlays


def test_now_next_osd_matches_playing_file_on_mixed_channels(tmp_path, monkeypatch):
    import nostalgiabox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    pool = tmp_path / "Sample Channel"
    pool.mkdir()
    for i in range(1, 5):
        (pool / f"s1e{i}.mp4").write_bytes(b"x")
    config = config_from_dict(
        {
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "state_path": str(tmp_path / "map.json"),
            "start_channel": 1,
            "tune_in": "broadcast",
            "start_offset": 0,
            "bridge_seconds": 0,
            "power_off_command": [],
        }
    )
    player = MockPlayer()
    app = TVApp(config, player, InputManager([]), clock=FakeClock())
    app.start()
    seen_now = set()
    for _ in range(4):
        playing = player.current
        assert playing is not None
        ass = player.overlays[5]
        now_name, next_name = app.lineup.current.guide_filenames(playing)
        assert f"NOW  {playing.stem}" in ass
        assert f"NEXT  {next_name}" in ass
        assert now_name == playing.stem
        seen_now.add(playing.name)
        send(app, Action.CHANNEL_UP)
    assert len(seen_now) >= 2


def test_channel_up_down_wraps(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 3
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 4
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2  # wrapped
    send(app, Action.CHANNEL_DOWN)
    assert app.lineup.current.number == 4  # wrapped back


def test_volume_controls(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 100 and player.volume == 100
    send(app, Action.VOLUME_DOWN)
    assert app.volume == 95
    # volume overlay was drawn
    assert "Volume" in player.overlays[2]


def test_volume_clamps(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=98, volume_step=5)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 100
    for _ in range(30):
        send(app, Action.VOLUME_DOWN)
    assert app.volume == 0


def test_volume_down_at_zero_does_not_power_off(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=10, volume_step=5)
    app.start()
    send(app, Action.VOLUME_DOWN)
    send(app, Action.VOLUME_DOWN)
    assert app.volume == 0 and not app.powered_off
    send(app, Action.VOLUME_DOWN)
    assert app.powered_off is False
    assert app.volume == 0


def test_power_off_disabled(tmp_path):
    app, player, _ = build_app(
        tmp_path, initial_volume=0, power_off_on_min_volume=False
    )
    app.start()
    send(app, Action.VOLUME_DOWN)   # at 0, but feature disabled
    assert app.powered_off is False


def test_mute_toggle_and_unmute_on_volume(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.MUTE)
    assert app.muted and player.muted
    send(app, Action.VOLUME_UP)  # changing volume unmutes
    assert not app.muted and not player.muted


def test_direct_channel_entry_with_enter(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 4)
    assert app.lineup.current.number == 2  # not committed yet
    send(app, Action.ENTER)
    assert app.lineup.current.number == 4


def test_direct_channel_entry_times_out(tmp_path):
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 3)
    assert app.lineup.current.number == 2
    clock.advance(2.1)  # past the entry timeout
    app.step()
    assert app.lineup.current.number == 3


def test_invalid_channel_entry_shows_message(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.select_channel_number(99) is False
    assert "NO CHANNEL" in player.overlays.get(4, "")
    assert app.lineup.current.number == 2  # unchanged


def test_last_channel_jump(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)  # now on 3, last=2
    assert app.lineup.current.number == 3
    send(app, Action.LAST_CHANNEL)
    assert app.lineup.current.number == 2
    send(app, Action.LAST_CHANNEL)  # bounces back to 3
    assert app.lineup.current.number == 3


def test_episode_advances_on_end(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    first = player.current
    player.finish_current(END_EOF)  # simulate the episode ending
    app._drain_playback_events()
    assert player.current is not None
    assert player.current != first  # rolled into the next shuffled episode


def test_standby_blanks_and_ignores_input(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.POWER)
    assert app.standby
    assert player.current is None  # screen blanked
    assert 3 in player.overlays  # standby overlay
    # input is ignored while in standby
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2
    # power again wakes it up and resumes playback
    send(app, Action.POWER)
    assert not app.standby
    assert player.current is not None


class _Hdmi:
    def __init__(self, connected=True):
        self.connected = connected

    def __call__(self):
        return self.connected


def test_hdmi_lost_does_not_stop_before_timeout(tmp_path):
    hdmi = _Hdmi(True)
    app, player, clock = build_app(tmp_path, hdmi_idle_pause_seconds=600)
    app._hdmi_signal = hdmi
    app.start()
    hdmi.connected = False
    app.step()
    clock.advance(599)
    app.step()
    assert player.current is not None
    assert app.hdmi_idle is False


def test_hdmi_lost_stops_after_timeout_and_wakes_on_signal(tmp_path):
    hdmi = _Hdmi(True)
    app, player, clock = build_app(
        tmp_path, hdmi_idle_pause_seconds=600, tune_in="broadcast"
    )
    app._hdmi_signal = hdmi
    app.start()
    first_channel = app.lineup.current.number
    hdmi.connected = False
    app.step()
    clock.advance(600)
    app.step()
    assert player.current is None
    assert app.hdmi_idle is True
    assert app.lineup.current.number == first_channel
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert player.current is None  # idle: do not roll episodes
    hdmi.connected = True
    app.step()
    assert app.hdmi_idle is False
    assert player.current is not None
    assert app.lineup.current.number == first_channel


def test_hdmi_idle_channel_change_does_not_play_until_wake(tmp_path):
    hdmi = _Hdmi(False)
    app, player, clock = build_app(tmp_path, hdmi_idle_pause_seconds=10)
    app._hdmi_signal = hdmi
    app.start()
    app.step()
    clock.advance(10)
    app.step()
    assert app.hdmi_idle
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 3
    assert player.current is None
    hdmi.connected = True
    app.step()
    assert player.current is not None
    assert app.lineup.current.number == 3


def test_hdmi_idle_disabled(tmp_path):
    hdmi = _Hdmi(False)
    app, player, clock = build_app(tmp_path, hdmi_idle_pause_seconds=0)
    app._hdmi_signal = hdmi
    app.start()
    clock.advance(10_000)
    app.step()
    assert player.current is not None
    assert app.hdmi_idle is False


def test_usb_unplug_rescans_and_replug_keeps_show(tmp_path):
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    (show / "s01e01.mp4").write_bytes(b"x")
    config = config_from_dict(
        {
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "state_path": str(tmp_path / "map.json"),
            "start_channel": 1,
            "tune_in": "broadcast",
            "start_offset": 0,
            "bridge_seconds": 0,
            "power_off_command": [],
        }
    )
    clock = FakeClock()
    player = MockPlayer()
    app = TVApp(config, player, InputManager([]), clock=clock)
    app.start()
    number = app.lineup.current.number
    assert app.lineup.current.name.lower() == "stitch"
    pool.rename(tmp_path / "usb-away")
    app.step()
    assert app.lineup.current.number == number
    assert app.lineup.current.is_empty
    (tmp_path / "usb-away").rename(pool)
    app.step()
    assert app.lineup.current.number == number
    assert app.lineup.current.name.lower() == "stitch"
    assert player.current is not None


def test_finishing_last_episode_picks_up_new_file(tmp_path, monkeypatch):
    import nostalgiabox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 10.0)
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    (show / "s01e01.mp4").write_bytes(b"x")
    config = config_from_dict(
        {
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "state_path": str(tmp_path / "map.json"),
            "start_channel": 1,
            "tune_in": "broadcast",
            "start_offset": 0,
            "bridge_seconds": 0,
            "power_off_command": [],
        }
    )
    player = MockPlayer()
    app = TVApp(config, player, InputManager([]), clock=FakeClock())
    app.start()
    (show / "s01e02.mp4").write_bytes(b"x")
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert player.current is not None
    assert player.current.name == "s01e02.mp4"


def test_nightly_rescan_adds_files_without_stopping_current(tmp_path):
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    first = show / "s01e01.mp4"
    first.write_bytes(b"x")

    class Wall:
        def __init__(self):
            self.dt = datetime(2026, 9, 1, 3, 0, 0)

        def __call__(self):
            return self.dt

    wall = Wall()
    config = config_from_dict(
        {
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "state_path": str(tmp_path / "map.json"),
            "start_channel": 1,
            "tune_in": "broadcast",
            "start_offset": 0,
            "bridge_seconds": 0,
            "library_rescan_hour": 4,
            "power_off_command": [],
        }
    )
    player = MockPlayer()
    app = TVApp(
        config, player, InputManager([]), clock=FakeClock(), wall_clock=wall
    )
    app.start()
    playing = player.current
    assert playing is not None
    (show / "s01e02.mp4").write_bytes(b"x")
    app.step()
    names_before = {p.name for c in app.lineup for p in c.episodes}
    assert "s01e02.mp4" not in names_before
    wall.dt = datetime(2026, 9, 1, 4, 0, 0)
    app.step()
    names_after = {p.name for c in app.lineup for p in c.episodes}
    assert "s01e02.mp4" in names_after
    assert player.current == playing


def test_nightly_rescan_skipped_when_hdmi_playing(tmp_path):
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    (show / "s01e01.mp4").write_bytes(b"x")

    class Wall:
        def __init__(self):
            self.dt = datetime(2026, 9, 1, 3, 0, 0)

        def __call__(self):
            return self.dt

    wall = Wall()
    config = config_from_dict(
        {
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "state_path": str(tmp_path / "map.json"),
            "start_channel": 1,
            "tune_in": "broadcast",
            "start_offset": 0,
            "bridge_seconds": 0,
            "library_rescan_hour": 4,
            "power_off_command": [],
        }
    )
    player = MockPlayer()
    app = TVApp(
        config, player, InputManager([]), clock=FakeClock(), wall_clock=wall
    )
    app._hdmi_signal = lambda: True
    app.start()
    (show / "s01e02.mp4").write_bytes(b"x")
    wall.dt = datetime(2026, 9, 1, 4, 0, 0)
    app.step()
    names = {p.name for c in app.lineup for p in c.episodes}
    assert "s01e02.mp4" not in names


def test_quit_stops_running(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app._running = True
    send(app, Action.QUIT)
    assert app._running is False


def test_glitch_transition_then_episode(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, clock = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    send(app, Action.CHANNEL_UP)
    # A glitch->episode transition was issued (glitch clip + preloaded episode).
    assert player.transitions, "expected a transition on channel change"
    clip, target, _start = player.transitions[-1]
    assert clip == assets / "glitch.mp4"
    assert player.current == target  # the episode is what plays


def test_transition_none_cuts_straight(tmp_path):
    # bridge_seconds=0 -> switch immediately, no transition clip, no preload
    app, player, _ = build_app(tmp_path, transition="none", bridge_seconds=0)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert not player.transitions
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_channel_change_bridges_current_until_next_ready(tmp_path):
    # With bridge_seconds>0 and no transition, the current show keeps playing
    # while the next channel preloads, then cuts over after the window.
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert player.current == first          # old show still playing...
    assert player.preloaded is not None     # ...next channel preloading
    clock.advance(1.0)
    app.step()                              # bridge window elapsed -> switch
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_advance_within_channel_has_no_transition(tmp_path):
    # An episode ending should roll straight into the next one (no glitch burst).
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, _ = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    before = len(player.transitions)
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert len(player.transitions) == before  # no new transition
    assert player.current is not None


def test_start_offset_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=5)
    app.start()
    # The episode should begin 5 seconds in, not at the very beginning.
    assert player.played[-1][1] == 5.0


def test_start_offset_range_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=[6, 10])
    app.start()
    assert 6.0 <= player.played[-1][1] <= 10.0


def test_empty_channel_shows_no_signal(tmp_path):
    (tmp_path / "dragon").mkdir()
    make_show(tmp_path, "arthur", 2)
    config = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            ]
        }
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()  # starts on ch 2 which is empty
    assert "Ovaj kanal nema danas crtaća" in app.player.overlays.get(4, "")


def test_empty_channel_message_clears_when_leaving(tmp_path):
    (tmp_path / "dragon").mkdir()
    make_show(tmp_path, "arthur", 2)
    config = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            ],
            "bridge_seconds": 0,
        }
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()
    send(app, Action.CHANNEL_UP)
    assert "Ovaj kanal nema danas crtaća" not in app.player.overlays.get(4, "")
    assert 4 not in app.player.overlays


def test_channel_banner_deferred_until_switch(tmp_path):
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    player.overlays.pop(1, None)          # clear the power-on banner
    send(app, Action.CHANNEL_UP)
    assert 1 not in player.overlays       # banner NOT shown during the bridge
    clock.advance(1.0)
    app.step()                            # cut-over happens here
    assert "CH 03" in player.overlays.get(1, "")  # banner appears at the switch


def test_resume_mode_restarts_where_left(tmp_path):
    # bridge_seconds=0 keeps this test focused on resume (immediate switches)
    app, player, _ = build_app(tmp_path, tune_in="resume", bridge_seconds=0)
    app.start()
    playing = player.current
    player.time_pos = 42.0
    send(app, Action.CHANNEL_UP)  # leave ch 2, remembering position 42
    send(app, Action.CHANNEL_DOWN)  # back to ch 2 -> resume at 42
    assert player.current == playing
    assert player.played[-1] == (playing, 42.0)
