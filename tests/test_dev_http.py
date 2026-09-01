import urllib.error
import urllib.request

from nostalgiabox.actions import Action
from nostalgiabox.input.dev_http import path_to_event
from nostalgiabox.input.manager import InputManager, create_backends


def test_path_to_event_known_routes():
    assert path_to_event("/channel/up").action == Action.CHANNEL_UP
    assert path_to_event("/channel/down").action == Action.CHANNEL_DOWN
    assert path_to_event("/volume/up").action == Action.VOLUME_UP
    assert path_to_event("/volume/down").action == Action.VOLUME_DOWN
    assert path_to_event("/mute").action == Action.MUTE
    assert path_to_event("/info").action == Action.INFO
    assert path_to_event("/last").action == Action.LAST_CHANNEL
    assert path_to_event("/power").action == Action.POWER
    assert path_to_event("/quit").action == Action.QUIT


def test_path_to_event_trailing_slash_and_digit():
    assert path_to_event("/channel/up/").action == Action.CHANNEL_UP
    ev = path_to_event("/digit/7")
    assert ev.action == Action.DIGIT and ev.value == 7


def test_path_to_event_unknown():
    assert path_to_event("/health") is None
    assert path_to_event("/nope") is None
    assert path_to_event("/digit/42") is None


def test_create_backends_skips_dev_http_by_default():
    backends = create_backends({"keyboard": False, "cec": False, "stdin": False})
    assert backends == []


def test_dev_http_health_and_channel_up():
    backends = create_backends(
        {
            "keyboard": False,
            "cec": False,
            "stdin": False,
            "dev_http": True,
            "dev_http_port": 0,
        }
    )
    assert len(backends) == 1
    mgr = InputManager(backends)
    mgr.start()
    try:
        port = backends[0].port
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
            assert resp.status == 200
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/channel/up", method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status in (200, 204)
        event = mgr.get(timeout=1.0)
        assert event is not None
        assert event.action == Action.CHANNEL_UP
    finally:
        mgr.stop()


def test_dev_http_unknown_path_is_404():
    backends = create_backends(
        {
            "keyboard": False,
            "cec": False,
            "stdin": False,
            "dev_http": True,
            "dev_http_port": 0,
        }
    )
    mgr = InputManager(backends)
    mgr.start()
    try:
        port = backends[0].port
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/nope", method="POST"
                )
            )
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        mgr.stop()
