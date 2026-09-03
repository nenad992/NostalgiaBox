"""Fixes for broadcast TV behaviour, library edge cases, and OSD/docs mismatches."""

import random
from datetime import datetime
from pathlib import Path

from nostalgiabox.actions import Action
from nostalgiabox.app import TVApp
from nostalgiabox.channel import (
    Channel,
    build_lineup,
    channel_rng_seed,
    deal_episodes,
    scan_episodes,
)
from nostalgiabox.config import config_from_dict
from nostalgiabox.input.keyboard import skip_evdev_device
from nostalgiabox.input.manager import InputManager
from nostalgiabox.overlay import OverlayManager, _escape
from nostalgiabox.player import END_EOF, MockPlayer, should_emit_eof
from tests.helpers import FakeClock, make_show
from tests.test_app import build_app, send


def test_play_after_does_not_skip_the_start_of_the_next_episode(tmp_path):
    folder = make_show(tmp_path, "arthur", 3)
    from nostalgiabox.config import ChannelConfig

    eps = scan_episodes(folder, [".mp4"])
    ch = Channel(
        ChannelConfig(number=3, name="arthur", path=folder),
        eps,
        tune_in="random",
        start_offset_min=6.0,
        start_offset_max=10.0,
        rng=random.Random(0),
    )
    nxt = ch.play_after(eps[0])
    assert nxt is not None
    assert nxt.start == 0.0
    assert nxt.path == eps[1]


def test_broadcast_eof_rolls_to_next_file_at_zero(tmp_path, monkeypatch):
    import nostalgiabox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    folder = make_show(tmp_path, "arthur", 3)
    from nostalgiabox.config import ChannelConfig

    eps = scan_episodes(folder, [".mp4"])
    ch = Channel(
        ChannelConfig(number=3, name="arthur", path=folder),
        eps,
        tune_in="broadcast",
        start_offset_min=6.0,
        start_offset_max=10.0,
        broadcast_epoch=0.0,
        rng=random.Random(0),
    )
    ch.tune_in(now=0.0)
    nxt = ch.play_after(eps[0])
    assert nxt is not None
    assert nxt.path == eps[1]
    assert nxt.start == 0.0


def test_channel_rng_seed_stable_unlike_builtin_hash():
    a = channel_rng_seed(7, 2, 0)
    b = channel_rng_seed(7, 2, 0)
    assert a == b
    assert channel_rng_seed(7, 2, 0) != channel_rng_seed(8, 2, 0)
    assert channel_rng_seed(7, 2, 0) != channel_rng_seed(7, 2, 1)


def test_dedicated_usb_unplug_is_detected(tmp_path):
    show = make_show(tmp_path, "arthur", 2)
    config = config_from_dict(
        {
            "channels": [{"number": 2, "name": "Arthur", "path": str(show)}],
            "start_offset": 0,
            "bridge_seconds": 0,
            "power_off_command": [],
        }
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()
    assert app.player.current is not None
    show.rename(tmp_path / "arthur-away")
    app.step()
    assert app.lineup.current.is_empty
    (tmp_path / "arthur-away").rename(show)
    app.step()
    assert not app.lineup.current.is_empty
    assert app.player.current is not None


def test_resume_survives_nightly_rescan(tmp_path):
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    first = show / "s01e01.mp4"
    first.write_bytes(b"x")
    (show / "s01e02.mp4").write_bytes(b"x")

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
            "tune_in": "resume",
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
    player.time_pos = 42.0
    send(app, Action.CHANNEL_UP)
    wall.dt = datetime(2026, 9, 1, 4, 0, 0)
    app.step()
    send(app, Action.CHANNEL_DOWN)
    assert player.played[-1][1] == 42.0


def test_scan_episodes_survives_vanishing_root(tmp_path, monkeypatch):
    folder = make_show(tmp_path, "arthur", 1)

    def boom(self, *args, **kwargs):
        raise OSError("unplugged")

    monkeypatch.setattr(Path, "rglob", boom)
    assert scan_episodes(folder, [".mp4"]) == []


def test_check_counts_unique_files(tmp_path, capsys):
    from nostalgiabox.__main__ import _cmd_check

    pool = tmp_path / "pool"
    pool.mkdir()
    for i in range(4):
        (pool / f"ep{i}.mp4").write_bytes(b"x")
    cfg = config_from_dict(
        {"mixed": {"path": str(pool), "count": 10, "first_number": 1}}
    )
    assert _cmd_check(cfg) == 0
    out = capsys.readouterr().out
    assert "unique files: 4" in out
    assert "total episode slots: 4" in out


def test_removed_show_frees_channel_when_library_still_present(tmp_path):
    stitch = tmp_path / "Stitch"
    poke = tmp_path / "Pokemon"
    stitch.mkdir()
    poke.mkdir()
    (stitch / "e01.mp4").write_bytes(b"x")
    (poke / "e01.mp4").write_bytes(b"x")
    mapping = {"stitch": 1, "pokemon": 2}
    files = list(poke.iterdir())
    deal_episodes(
        files,
        10,
        random.Random(0),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    assert "stitch" not in mapping
    assert mapping["pokemon"] == 2


def test_empty_pool_does_not_wipe_sticky_map(tmp_path):
    mapping = {"stitch": 1}
    deal_episodes(
        [],
        10,
        random.Random(0),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    assert mapping["stitch"] == 1


def test_skip_cec_like_evdev_names():
    assert skip_evdev_device("Pulse-Eight USB-CEC Adapter") is True
    assert skip_evdev_device("Flirc USB Receiver") is False
    assert skip_evdev_device("HID Keyboard") is False
    assert skip_evdev_device("vc4-hdmi-0") is False
    assert skip_evdev_device("vc4-hdmi0 HDMI CEC") is False
    assert skip_evdev_device("vc4-hdmi-0 HDMI Jack") is False


def test_vc4_hdmi_remote_name():
    from nostalgiabox.input.keyboard import is_vc4_hdmi_remote_name

    assert is_vc4_hdmi_remote_name("vc4-hdmi-0") is True
    assert is_vc4_hdmi_remote_name("vc4-hdmi-1") is True
    assert is_vc4_hdmi_remote_name("vc4-hdmi-0 HDMI Jack") is False
    assert is_vc4_hdmi_remote_name("Flirc") is False


def test_transition_static_eof_is_ignored_then_episode_eof_counts():
    report, ignore = should_emit_eof(suppress=False, ignore_next_eof=True)
    assert report is False and ignore is False
    report, ignore = should_emit_eof(suppress=False, ignore_next_eof=False)
    assert report is True and ignore is False
    report, ignore = should_emit_eof(suppress=True, ignore_next_eof=True)
    assert report is False and ignore is True


def test_volume_empty_segments_use_dim_color(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict(
        {
            "channels": [{"number": 2, "name": "A", "path": str(tmp_path / "a")}],
            "ui": {"dim_color": "#123B18"},
        }
    )
    player = MockPlayer()
    OverlayManager(player, cfg, clock=FakeClock()).show_volume(0, muted=False)
    assert "&H00183B12" in player.overlays[2]


def test_escape_newlines_in_osd_text():
    assert "\n" not in _escape("foo\nbar")
    assert "foo bar" in _escape("foo\nbar")


def test_shuffle_false_plays_episodes_in_order(tmp_path):
    folder = make_show(tmp_path, "arthur", 4)
    from nostalgiabox.config import ChannelConfig

    eps = scan_episodes(folder, [".mp4"])
    ch = Channel(
        ChannelConfig(number=3, name="arthur", path=folder, shuffle=False),
        eps,
        tune_in="random",
        rng=random.Random(99),
    )
    seen = [ch.tune_in().path]
    for _ in range(3):
        seen.append(ch.advance().path)
    assert seen == eps


def test_nightly_keep_playback_retunes_if_file_gone(tmp_path):
    pool = tmp_path / "usb"
    show = pool / "Stitch"
    show.mkdir(parents=True)
    first = show / "s01e01.mp4"
    first.write_bytes(b"x")
    (show / "s01e02.mp4").write_bytes(b"x")

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
    first.unlink()
    wall.dt = datetime(2026, 9, 1, 4, 0, 0)
    app.step()
    assert player.current is not None
    assert player.current.name == "s01e02.mp4"


def test_app_eof_starts_next_episode_at_zero(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=5)
    app.start()
    assert player.played[-1][1] == 5.0
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert player.played[-1][1] == 0.0
