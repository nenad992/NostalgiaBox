"""Best-effort media duration probing via ffprobe.

Only the optional "broadcast" tune-in mode needs to know how long each episode
runs (so it can pretend the channel has been airing continuously). Probing is
done once at startup and is entirely best-effort: if ffprobe is missing or a
file cannot be read, we fall back to an assumed episode length so the box still
works.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# A typical kids' TV episode is about 22 minutes; used when we cannot probe.
DEFAULT_EPISODE_SECONDS = 22 * 60.0

_cache: Optional[Dict[str, Any]] = None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def duration_cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "nostalgiabox" / "durations.json"


def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = duration_cache_path()
    try:
        _cache = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(_cache, dict):
            _cache = {}
    except (OSError, ValueError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    path = duration_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_cache), encoding="utf-8")
    except OSError:
        pass


def probe_duration(path: Path, *, timeout: float = 15.0) -> Optional[float]:
    """Return the duration of ``path`` in seconds, or ``None`` on failure."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cache = _load_cache()
    hit = cache.get(key)
    if (
        isinstance(hit, dict)
        and hit.get("mtime") == st.st_mtime
        and hit.get("size") == st.st_size
    ):
        try:
            value = float(hit["duration"])
        except (TypeError, ValueError, KeyError):
            value = 0.0
        if value > 0:
            return value

    if not ffprobe_available():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        duration = data.get("format", {}).get("duration")
        if duration is None:
            return None
        value = float(duration)
        if value <= 0:
            return None
        cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "duration": value}
        _save_cache()
        return value
    except (subprocess.SubprocessError, ValueError, OSError, json.JSONDecodeError):
        return None


__all__ = ["probe_duration", "ffprobe_available", "DEFAULT_EPISODE_SECONDS"]
