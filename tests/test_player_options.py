import pytest

from nostalgiabox.player import mpv_player_options


def test_force_window_yes_is_valid_for_libmpv():
    opts = mpv_player_options(fullscreen=True)
    assert opts["force_window"] == "yes"
    assert opts["idle"] == "yes"
    assert opts["panscan"] == 1.0
    assert "vf" not in opts
    assert "geometry" not in opts


def test_force_4_3_letterbox_drops_panscan():
    opts = mpv_player_options(force_4_3=True)
    assert "panscan" not in opts
    assert "pad=960:720" in opts["vf"]


def test_windowed_mode_sets_visible_geometry():
    opts = mpv_player_options(fullscreen=False)
    assert opts["fullscreen"] is False
    assert opts["geometry"] == "1280x720"
    assert opts["title"] == "NostalgiaBox"


def test_windowed_options_initialize_libmpv():
    mpv = pytest.importorskip("mpv")
    try:
        player = mpv.MPV(**mpv_player_options(fullscreen=False, force_4_3=False))
    except OSError:
        pytest.skip("libmpv not available")
    player.terminate()
