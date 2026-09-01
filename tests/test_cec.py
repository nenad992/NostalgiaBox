from nostalgiabox.actions import Action
from nostalgiabox.input.cec import parse_cec_line
from nostalgiabox.input.keymap import cec_key_to_event


def test_cec_named_keys():
    assert cec_key_to_event("channel up").action == Action.CHANNEL_UP
    assert cec_key_to_event("Volume Down") is None
    assert cec_key_to_event("mute") is None
    assert cec_key_to_event("left") is None
    assert cec_key_to_event("right") is None
    assert cec_key_to_event("select").action == Action.ENTER
    assert cec_key_to_event("number 4").value == 4
    assert cec_key_to_event("power").action == Action.POWER
    assert cec_key_to_event("page up").action == Action.CHANNEL_UP
    assert cec_key_to_event("ch down").action == Action.CHANNEL_DOWN
    assert cec_key_to_event("nonsense") is None


def test_parse_cec_client_key_pressed_line():
    ev = parse_cec_line("DEBUG:   key pressed: up (1)")
    assert ev is not None and ev.action == Action.CHANNEL_UP
    assert parse_cec_line("TRAFFIC: [123] key pressed: volume up (41)") is None
    assert parse_cec_line("key released: up (1)") is None


def test_parse_cec_user_control_pressed_hex():
    # User Control Pressed (0x44) + Up (0x01), Down (0x02), Volume Up (0x41)
    ev = parse_cec_line("TRAFFIC: [  12345]	>> 01:44:01")
    assert ev is not None and ev.action == Action.CHANNEL_UP
    ev = parse_cec_line(">> 01:44:30")  # Channel Up
    assert ev is not None and ev.action == Action.CHANNEL_UP
    ev = parse_cec_line(">> 01:44:31")  # Channel Down
    assert ev is not None and ev.action == Action.CHANNEL_DOWN
    ev = parse_cec_line(">> 01:44:41")
    assert ev is None
    ev = parse_cec_line(">> 01:44:42")
    assert ev is None
    ev = parse_cec_line(">> 01:44:03")
    assert ev is None
    ev = parse_cec_line(">> 01:44:04")
    assert ev is None
    assert parse_cec_line(">> 01:45:01") is None  # released
    ev = parse_cec_line(">> 0f:44:20")
    assert ev is not None and ev.value == 0
