from nostalgiabox.macos_cocoa import is_macos, pump
from nostalgiabox.player import MockPlayer


def test_mock_player_pump_events_is_safe():
    MockPlayer().pump_events(0.0)


def test_cocoa_pump_does_not_raise():
    # On Linux/Pi this is a no-op; on macOS it attaches NSApplication once.
    pump(0.0)
    assert isinstance(is_macos(), bool)
