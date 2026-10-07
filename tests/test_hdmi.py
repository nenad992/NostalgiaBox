from pathlib import Path

from nostalgiabox.hdmi import drm_hdmi_connected


def _connector(root: Path, name: str, status: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "status").write_text(status + "\n")


def test_no_drm_dir_is_unknown(tmp_path):
    assert drm_hdmi_connected(tmp_path / "missing") is None


def test_no_hdmi_connectors_is_unknown(tmp_path):
    (tmp_path / "card0-VGA-1").mkdir()
    (tmp_path / "card0-VGA-1" / "status").write_text("connected\n")
    assert drm_hdmi_connected(tmp_path) is None


def test_hdmi_connected_vs_disconnected(tmp_path):
    _connector(tmp_path, "card1-HDMI-A-1", "disconnected")
    _connector(tmp_path, "card1-HDMI-A-2", "connected")
    assert drm_hdmi_connected(tmp_path) is True
    # only disconnected HDMI
    root = tmp_path / "only"
    _connector(root, "card1-HDMI-A-1", "disconnected")
    assert drm_hdmi_connected(root) is False


from nostalgiabox.hdmi import TvPowerWatcher, parse_power_status, tv_signal


def test_parse_power_status():
    assert parse_power_status("\tpwr-state: on (0x00)") is True
    assert parse_power_status("\tpwr-state: to-on (0x02)") is True
    assert parse_power_status("\tpwr-state: standby (0x01)") is False
    assert parse_power_status("\tpwr-state: to-standby (0x03)") is False
    assert parse_power_status("Tx, Not Acknowledged (2), Max Retries") is False
    assert parse_power_status("Tx, OK, Rx, Timeout") is None
    assert parse_power_status("") is None


def test_tv_signal_either_off_means_off():
    watcher = TvPowerWatcher()
    assert tv_signal(lambda: True, watcher)() is True        # CEC unknown yet
    watcher.tv_on = False
    assert tv_signal(lambda: True, watcher)() is False       # hotplug lies in standby
    watcher.tv_on = True
    assert tv_signal(lambda: False, watcher)() is False      # cable unplugged
    assert tv_signal(lambda: None, watcher)() is None
    assert tv_signal(lambda: True, None)() is True


def test_watcher_wakes_only_on_off_to_on(monkeypatch):
    woke = []
    watcher = TvPowerWatcher(interval=0, on_wake=lambda: woke.append(1))
    states = iter([True, False, None, True])

    def poll():
        try:
            return next(states)
        except StopIteration:
            watcher.stop()
            return None

    monkeypatch.setattr(watcher, "poll_once", poll)
    watcher._run()
    assert woke == [1]          # first "on" at boot does not count as a wake
    assert watcher.tv_on is True
