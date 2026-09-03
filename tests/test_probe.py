import json

import nostalgiabox.probe as probe_mod
from nostalgiabox.probe import probe_duration


def test_probe_duration_uses_cache(tmp_path, monkeypatch):
    probe_mod._cache = None
    cache_file = tmp_path / "durations.json"
    monkeypatch.setattr(probe_mod, "duration_cache_path", lambda: cache_file)
    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: False)

    video = tmp_path / "ep.mp4"
    video.write_bytes(b"x" * 10)
    st = video.stat()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({str(video): {"mtime": st.st_mtime, "size": st.st_size, "duration": 12.5}})
    )

    assert probe_duration(video) == 12.5
    probe_mod._cache = None
