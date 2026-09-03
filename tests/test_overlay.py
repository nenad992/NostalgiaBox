import re

from nostalgiabox.config import config_from_dict
from nostalgiabox.overlay import OverlayManager
from nostalgiabox.player import MockPlayer
from tests.helpers import FakeClock, make_show

# OSD uses the full 1280x720 canvas with an 8% safe inset (rounded-corner margin).
_SAFE_X0, _SAFE_X1 = 102, 1178


def _all_x_positions(ass: str):
    return [int(m) for m in re.findall(r"\\pos\((\d+),", ass)]


def _config(tmp_path):
    make_show(tmp_path, "a", 1)
    return config_from_dict(
        {
            "channel_bug_seconds": 5,
            "osd_duration": 2,
            "channels": [{"number": 3, "name": "Arthur", "path": str(tmp_path / "a")}],
        }
    )


def test_channel_bug_drawn_and_expires(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)

    om.show_channel_bug(3, "Arthur")
    assert 1 in player.overlays  # channel overlay id
    ass = player.overlays[1]
    assert "CH 03" in ass and "Arthur" in ass
    assert "\\fs104" in ass
    assert "\\fs48" in ass
    assert "\\p1" in ass  # dark backing plate
    assert "\\pos(778,47)" in ass  # tighter plate than the old 540x210 box
    assert "\\blur4" not in ass
    assert "\\bord3" in ass

    clock.advance(4.9)
    om.tick()
    assert 1 in player.overlays  # not yet expired

    clock.advance(0.2)
    om.tick()
    assert 1 not in player.overlays  # expired after 5s


def test_volume_overlay_has_label_and_bars(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=False)
    ass = player.overlays[2]
    assert "Volume" in ass
    # 20 segments: some drawn as bars (rectangles start "m 0 0 l"), rest as dots.
    assert ass.count("\\p1") == 20


def test_volume_bars_scale_with_level(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)
    full = player.overlays[2].count("m 0 0 l")  # rectangle (filled bar) count
    om.show_volume(0, muted=False)
    empty = player.overlays[2].count("m 0 0 l")
    assert full == 20 and empty == 0


def test_muted_volume_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=True)
    assert "Mute" in player.overlays[2]


def test_standby_overlay_does_not_expire(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_standby()
    clock.advance(1000)
    om.tick()
    assert 3 in player.overlays  # standby id persists
    om.clear_standby()
    assert 3 not in player.overlays


def test_channel_name_with_braces_is_escaped(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(5, "Weird{name}")
    # Braces in the name must be neutralised (they delimit ASS override blocks).
    ass = player.overlays[1]
    assert "Weird(name)" in ass
    assert "Weird{name}" not in ass


def test_message_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_message("CH 12  -  NO CHANNEL")
    assert "NO CHANNEL" in player.overlays[4]


def test_channel_bug_sits_in_fullscreen_safe_area(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    xs = _all_x_positions(player.overlays[1])
    assert xs and all(_SAFE_X0 <= x <= _SAFE_X1 for x in xs)


def test_volume_bar_sits_in_fullscreen_safe_area(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)
    xs = _all_x_positions(player.overlays[2])
    assert xs and all(_SAFE_X0 <= x <= _SAFE_X1 for x in xs)


def test_overlay_uses_configured_font_and_color(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    ass = player.overlays[1]
    assert "\\fnVT323" in ass          # bundled retro font
    assert "&H005AFF4D" in ass         # #4DFF5A -> ASS BBGGRR


def test_guide_sits_above_bottom_safe_edge(tmp_path):
    from nostalgiabox.overlay import CANVAS_H

    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_guide("now_file", "next_file")
    ys = [int(m) for m in re.findall(r"\\pos\(\d+,(\d+)\)", player.overlays[5])]
    inset = int(CANVAS_H * 0.08)
    iy1 = CANVAS_H - inset
    assert ys and min(ys) > inset and max(ys) < iy1
    assert "\\an8" in player.overlays[5]


def test_guide_shows_now_and_next_and_expires(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_guide("stitch_s01e01", "pokemon_s01e02")
    ass = player.overlays[5]
    assert "NOW  stitch_s01e01" in ass
    assert "NEXT  pokemon_s01e02" in ass
    assert "\\fs40" in ass
    assert "\\p1" in ass
    assert "\\blur4" not in ass
    clock.advance(4.9)
    om.tick()
    assert 5 in player.overlays
    clock.advance(0.2)
    om.tick()
    assert 5 not in player.overlays


def test_guide_fast_channel_change_replaces_and_rearms(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_guide("a", "b")
    clock.advance(4.0)
    om.show_guide("c", "d")
    assert "NOW  c" in player.overlays[5]
    assert "NOW  a" not in player.overlays[5]
    clock.advance(4.9)
    om.tick()
    assert 5 in player.overlays
    clock.advance(0.2)
    om.tick()
    assert 5 not in player.overlays


def test_lineup_lists_channels_and_marks_current(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_lineup(
        [
            (1, "Kanal 1", "lilo_e01", False),
            (2, "Kanal 2", "looney_e03", True),
        ]
    )
    ass = player.overlays[6]
    assert "CH 01" in ass and "Kanal 1" in ass and "lilo_e01" in ass
    assert "CH 02" in ass and ">" in ass
    assert "^" in ass and "v" in ass
    assert "\\fs36" in ass
    assert "\\p1" in ass
    assert "\\blur4" not in ass
    clock.advance(4.9)
    om.tick()
    assert 6 in player.overlays
    clock.advance(0.2)
    om.tick()
    assert 6 not in player.overlays
