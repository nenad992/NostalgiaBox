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
