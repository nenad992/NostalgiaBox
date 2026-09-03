"""Channels: the folders of episodes and how they decide what to play.

A :class:`Channel` wraps one show (a folder of episode files) and knows how to
answer two questions:

* "I just tuned in - what should I play?" (:meth:`Channel.tune_in`)
* "The episode ended - what's next?" (:meth:`Channel.advance`)

The answer depends on the configured ``tune_in`` mode (see ``config.py``):
random, resume, or broadcast. :class:`ChannelLineup` holds all the channels and
provides the up/down/by-number navigation a remote needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import AbstractSet, Dict, List, Optional, Sequence

_SEASON_EPISODE = (
    re.compile(r"s(\d{1,2})[ ._-]?e(\d{1,3})", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})x(\d{1,3})\b"),
    re.compile(r"v(\d{1,2})e(\d{1,3})", re.IGNORECASE),
)
_FRANCHISE_NOISE = re.compile(
    r"\([^)]*\)|\s*-\s*sinhronizovano\s*",
    re.IGNORECASE,
)
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_TRAILING_SEQUEL = re.compile(
    r"(?:[\s._-]+(?:part[\s._-]*)?(?:[1-9]|1[0-9]|ii|iii|iv|v|vi)|(?<=[a-z])[2-9])\s*$",
    re.IGNORECASE,
)
_NATURAL_SPLIT = re.compile(r"(\d+)")
_SEASON_PATTERNS = (
    re.compile(r"s(\d{1,2})[ ._-]?e\d{1,3}", re.IGNORECASE),   # S06E01, s6e1
    re.compile(r"\bseason[ ._-]*(\d{1,2})\b", re.IGNORECASE),  # Season 6
    re.compile(r"\b(\d{1,2})x\d{1,3}\b"),                       # 6x01
)

# Strip trailing episode markers so "Pokemon S01E02" and "pokemon_ep03" group.
_SERIES_TAIL = re.compile(
    r"[\s._-]*(?:s\d{1,2}[ ._-]?e\d{1,3}|\d{1,2}x\d{1,3}|ep(?:isode)?[\s._-]*\d+)$",
    re.IGNORECASE,
)

from .config import ChannelConfig, Config, _prettify_name
from .playlist import ShuffleBag
from .probe import DEFAULT_EPISODE_SECONDS, probe_duration

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayRequest:
    """An instruction to the player: play ``path`` starting at ``start`` sec."""

    path: Path
    start: float = 0.0


def detect_season(text: str) -> Optional[int]:
    """Best-effort extraction of a season number from a path/filename."""
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def scan_episodes(
    root: Path,
    extensions: Sequence[str],
    *,
    recursive: bool = True,
    exclude: Sequence[str] = (),
    exclude_seasons: AbstractSet[int] = frozenset(),
) -> List[Path]:
    """Return a sorted list of episode files under ``root``.

    Sorting is natural-ish (case-insensitive by full path) so that, in the rare
    cases we present episodes in order, they are at least stable. Hidden files
    and typical sidecar files are ignored.

    ``exclude`` is a list of case-insensitive glob patterns; any episode whose
    relative path or filename matches one is dropped. ``exclude_seasons`` drops
    episodes whose detected season number is in the set.
    """
    try:
        if not root.exists():
            log.warning("channel folder does not exist: %s", root)
            return []
        walker = root.rglob("*") if recursive else root.glob("*")
    except OSError:
        log.warning("could not scan channel folder: %s", root, exc_info=True)
        return []
    exts = {e.lower() for e in extensions}
    patterns = [p.lower() for p in exclude]
    episodes: List[Path] = []
    try:
        for p in walker:
            try:
                if not (
                    p.is_file()
                    and p.suffix.lower() in exts
                    and not p.name.startswith(".")
                    and not _is_excluded(p, root, patterns, exclude_seasons)
                ):
                    continue
            except OSError:
                continue
            episodes.append(p)
    except OSError:
        log.warning("scan interrupted for %s", root, exc_info=True)
        return []
    episodes.sort(key=episode_sort_key)
    return episodes


def _is_excluded(
    path: Path,
    root: Path,
    patterns: Sequence[str],
    exclude_seasons: AbstractSet[int],
) -> bool:
    import fnmatch

    try:
        rel = path.relative_to(root).as_posix().lower()
    except ValueError:  # pragma: no cover - path always under root here
        rel = path.name.lower()
    name = path.name.lower()
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    if exclude_seasons:
        season = detect_season(rel)
        if season is not None and season in exclude_seasons:
            return True
    return False


class BroadcastSchedule:
    """A never-ending, always-running shuffled running order for a channel.

    Given episode durations and a fixed start epoch, it can report exactly what
    "would be airing" at any wall-clock moment - the illusion that the station
    kept broadcasting while nobody was watching. Episodes air in show order
    (see :func:`air_order`) and the list loops forever.
    """

    def __init__(
        self,
        episodes: Sequence[Path],
        durations: Sequence[float],
        *,
        epoch: float,
        rng: random.Random | None = None,
    ) -> None:
        if len(episodes) != len(durations):
            raise ValueError("episodes and durations must be the same length")
        self._episodes = list(episodes)
        self._durations = [max(1.0, float(d)) for d in durations]
        self._epoch = epoch
        self._cycle = sum(self._durations)

    def at(self, when: float) -> PlayRequest:
        """What is airing at wall-clock time ``when`` (and how far into it)."""
        elapsed = (when - self._epoch) % self._cycle
        for path, dur in zip(self._episodes, self._durations):
            if elapsed < dur:
                return PlayRequest(path=path, start=elapsed)
            elapsed -= dur
        # Floating point rounding safety net.
        return PlayRequest(path=self._episodes[-1], start=0.0)

    def next_path(self, current: Path) -> Path:
        i = _index_of_path(self._episodes, current)
        if i < 0:
            return self._episodes[0]
        return self._episodes[(i + 1) % len(self._episodes)]


def _index_of_path(ordered: Sequence[Path], current: Path) -> int:
    """Find ``current`` in ``ordered`` even if the Path objects differ."""
    for i, path in enumerate(ordered):
        if path == current:
            return i
    try:
        target = current.resolve()
    except OSError:
        target = None
    if target is not None:
        for i, path in enumerate(ordered):
            try:
                if path.resolve() == target:
                    return i
            except OSError:
                continue
    names = [path.name for path in ordered]
    if names.count(current.name) == 1:
        return names.index(current.name)
    return -1


def franchise_key(name: str) -> str:
    """Collapse sequels/years so Inside Out 1 and 2 share one home channel."""
    text = _FRANCHISE_NOISE.sub(" ", name)
    text = text.replace("&", " ").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = _LEADING_ARTICLE.sub("", text.strip())
    while True:
        stripped = _TRAILING_SEQUEL.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text or name.strip().lower()


def episode_sort_key(path: Path) -> tuple:
    """S01E2 before S01E10 (not lexicographic s1e1, s1e10, s1e2)."""
    blob = f"{path.parent.name} {path.name}"
    for pattern in _SEASON_EPISODE:
        match = pattern.search(blob)
        if match:
            return (0, int(match.group(1)), int(match.group(2)), _natural_key(str(path)))
    return (1, 0, 0, _natural_key(str(path)))


def _natural_key(text: str) -> tuple:
    parts = _NATURAL_SPLIT.split(text.lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def series_prefix(stem: str) -> str:
    """Filename without trailing SxxExx / epNN / numbers, for grouping a show."""
    name = stem.strip()
    while True:
        stripped = _SERIES_TAIL.sub("", name).rstrip(" ._-")
        if stripped == name:
            break
        name = stripped
    return name if len(name) >= 2 else stem


def show_key(path: Path, root: Optional[Path] = None) -> str:
    """Which 'show' a file belongs to (subfolder, else series name in the file)."""
    if root is not None:
        try:
            rel = path.resolve().relative_to(root.resolve())
            if len(rel.parts) > 1:
                top = rel.parts[0]
                if detect_season(top) is None and not top.lower().startswith("season"):
                    return franchise_key(top)
            return series_prefix(path.stem).lower()
        except ValueError:
            pass
    parent = path.parent.name
    if parent and parent not in (".", "") and detect_season(parent) is None:
        if not parent.lower().startswith("season"):
            return franchise_key(parent)
    return series_prefix(path.stem).lower()


def air_order(
    episodes: Sequence[Path],
    rng: random.Random,
    *,
    root: Optional[Path] = None,
    block_size: int = 3,
) -> List[Path]:
    """Build a channel playlist: at most ``block_size`` episodes of one show, then another.

    Each show's episodes stay in filename order and resume after the break.
    A channel with only one show plays that show straight through.
    """
    groups: Dict[str, List[Path]] = {}
    for path in episodes:
        groups.setdefault(show_key(path, root), []).append(path)
    for key in groups:
        groups[key] = sorted(groups[key], key=episode_sort_key)
    keys = sorted(groups)
    if len(keys) <= 1:
        return list(groups[keys[0]]) if keys else []
    size = max(1, int(block_size))
    cursor = {key: 0 for key in keys}
    ordered: List[Path] = []
    while True:
        progressed = False
        for key in keys:
            start = cursor[key]
            files = groups[key]
            if start >= len(files):
                continue
            take = files[start : start + size]
            ordered.extend(take)
            cursor[key] = start + len(take)
            progressed = True
        if not progressed:
            break
    return ordered


def load_show_map(path: Optional[Path]) -> Dict[str, int]:
    """Persistent show-key -> channel number (Stitch stays on CH1)."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("shows", data) if isinstance(data, dict) else {}
    out: Dict[str, int] = {}
    if isinstance(raw, dict):
        for key, number in raw.items():
            try:
                out[str(key).lower()] = int(number)
            except (TypeError, ValueError):
                continue
    return out


def save_show_map(path: Optional[Path], mapping: Dict[str, int]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"shows": dict(sorted(mapping.items()))}, indent=2) + "\n"
        )
    except OSError:
        log.warning("could not save show map to %s", path, exc_info=True)


def deal_episodes(
    episodes: Sequence[Path],
    n_channels: int,
    rng: random.Random,
    *,
    root: Optional[Path] = None,
    mapping: Optional[Dict[str, int]] = None,
    channel_numbers: Optional[Sequence[int]] = None,
) -> List[List[Path]]:
    """Put each *show* on a sticky channel. New shows take the next empty slot.

    Already-mapped shows never move (Pokemon stays on CH1). Extra shows beyond
    the channel count share the least-full channel. ``mapping`` is updated
    in place when provided.
    """
    if n_channels < 1:
        raise ValueError("n_channels must be >= 1")
    numbers = list(channel_numbers) if channel_numbers is not None else list(
        range(1, n_channels + 1)
    )
    if len(numbers) != n_channels:
        raise ValueError("channel_numbers length must match n_channels")
    index_of = {number: i for i, number in enumerate(numbers)}
    valid = set(numbers)
    groups: Dict[str, List[Path]] = {}
    for path in episodes:
        groups.setdefault(show_key(path, root), []).append(path)
    owned: Dict[str, int] = {} if mapping is None else mapping
    if groups:
        for key in list(owned):
            if key not in groups or owned.get(key) not in valid:
                del owned[key]
    else:
        for key, number in list(owned.items()):
            if number not in valid:
                del owned[key]
    used = {n for n in owned.values() if n in valid}
    empty = [n for n in numbers if n not in used]
    new_keys = sorted(k for k in groups if k not in owned)
    for key in new_keys:
        if empty:
            owned[key] = empty.pop(0)
        else:
            # Every channel already has a show: park extras on the smallest pile.
            def _load(n: int) -> int:
                return sum(len(groups[k]) for k, ch in owned.items() if ch == n)

            owned[key] = min(numbers, key=_load)
        used.add(owned[key])
        empty = [n for n in numbers if n not in used]
    buckets: List[List[Path]] = [[] for _ in range(n_channels)]
    for key, files in groups.items():
        number = owned.get(key)
        if number is None:
            continue
        i = index_of[number]
        buckets[i].extend(sorted(files, key=lambda p: str(p).lower()))
    return buckets


def rotate_playlist_to_shows(
    playlist: Sequence[Path],
    home_keys: AbstractSet[str],
    root: Optional[Path] = None,
) -> List[Path]:
    """Rotate so the playlist starts at a home show; same files, same runtime."""
    items = list(playlist)
    if not items or not home_keys:
        return items
    for i, path in enumerate(items):
        if show_key(path, root) in home_keys:
            return items[i:] + items[:i]
    return items


def mixed_playlists(
    pool: Sequence[Path],
    home_buckets: Sequence[Sequence[Path]],
    *,
    root: Optional[Path] = None,
    block_size: int = 3,
) -> List[List[Path]]:
    """Each mixed channel airs only the shows dealt to it.

    Files are unique across channels, so two channels cannot play the same
    cartoon at the same time. Extra shows beyond 10 slots share a channel
    (see :func:`deal_episodes`). Empty slots stay empty.
    """
    del pool  # deal already assigned every pool file into home_buckets
    playlists: List[List[Path]] = []
    for home in home_buckets:
        files = list(home)
        if not files:
            playlists.append([])
            continue
        playlists.append(
            air_order(files, random.Random(0), root=root, block_size=block_size)
        )
    return playlists


def mixed_deal_seed(shuffle_seed: Optional[int], day_iso: str) -> int:
    """Stable 31-bit seed so the same day + config yields the same deal."""
    material = f"{shuffle_seed!s}|{day_iso}|mixed-deal".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % (2**31)


def channel_rng_seed(shuffle_seed: int, number: int, index: int) -> int:
    """Stable 31-bit seed (not Python's per-process ``hash()``)."""
    material = f"{shuffle_seed}|{number}|{index}|ch-rng".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % (2**31)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def start_of_local_day(when: Optional[float] = None) -> float:
    """Unix timestamp of local midnight (broadcast clock epoch)."""
    ts = time.time() if when is None else when
    dt = datetime.fromtimestamp(ts)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Channel:
    """A single TV channel backed by a folder of episodes."""

    def __init__(
        self,
        config: ChannelConfig,
        episodes: Sequence[Path],
        *,
        tune_in: str = "random",
        start_offset_min: float = 0.0,
        start_offset_max: Optional[float] = None,
        rng: Optional[random.Random] = None,
        broadcast_epoch: Optional[float] = None,
        show_block_episodes: int = 3,
    ) -> None:
        self.config = config
        self.episodes: List[Path] = list(episodes)
        self.tune_in_mode = tune_in
        # Start each episode a random number of seconds in (within this range) so
        # the picture appears already "in the show" and channel switches land at
        # varied points instead of always the same spot.
        self.start_offset_min = max(0.0, start_offset_min)
        self.start_offset_max = (
            self.start_offset_min
            if start_offset_max is None
            else max(self.start_offset_min, start_offset_max)
        )
        self._rng = rng or random.Random()
        self._block_size = max(1, int(show_block_episodes))
        self._broadcast_epoch = broadcast_epoch
        self._ordered = False
        self._bag: Optional[ShuffleBag[Path]] = None
        if self.episodes:
            if not self.config.shuffle:
                self._ordered = True
            else:
                self._bag = ShuffleBag(self.episodes, self._rng)
        self._order_index = 0
        # Resume state (used by the "resume" tune-in mode).
        self._resume_path: Optional[Path] = None
        self._resume_position: float = 0.0
        # Broadcast schedule (built lazily on first use in "broadcast" mode).
        self._broadcast: Optional[BroadcastSchedule] = None

    # -- identity -----------------------------------------------------------
    @property
    def number(self) -> int:
        return self.config.number

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_empty(self) -> bool:
        return not self.episodes

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Channel {self.number} {self.name!r} ({len(self.episodes)} eps)>"

    # -- playback selection -------------------------------------------------
    def _tune_in_offset(self) -> float:
        if self.start_offset_max > self.start_offset_min:
            return self._rng.uniform(self.start_offset_min, self.start_offset_max)
        return self.start_offset_min

    def _next_in_file_order(self) -> Path:
        path = self.episodes[self._order_index % len(self.episodes)]
        self._order_index += 1
        return path

    def _next_shuffled(self) -> PlayRequest:
        start = self._tune_in_offset()
        if self._ordered:
            return PlayRequest(path=self._next_in_file_order(), start=start)
        assert self._bag is not None
        return PlayRequest(path=self._bag.next(), start=start)

    def tune_in(self, *, now: Optional[float] = None) -> Optional[PlayRequest]:
        """Decide what to play the instant a viewer switches to this channel."""
        if self.is_empty:
            return None
        now = time.time() if now is None else now

        if self.tune_in_mode == "resume" and self._resume_path is not None:
            return PlayRequest(path=self._resume_path, start=self._resume_position)

        if self.tune_in_mode == "broadcast":
            schedule = self._ensure_broadcast(epoch=now)
            if schedule is not None:
                return schedule.at(now)
            # Fall through to random if the schedule could not be built.

        return self._next_shuffled()

    def advance(self) -> Optional[PlayRequest]:
        """Decide what to play when the current episode ends naturally."""
        if self.is_empty:
            return None
        if self.tune_in_mode == "broadcast" and self._broadcast is not None:
            # Roll straight into whatever airs next in the running order.
            return self._broadcast.at(time.time())
        return self._next_shuffled()

    def remember(self, path: Path, position: float) -> None:
        """Record where the viewer left off (for the "resume" mode)."""
        self._resume_path = path
        self._resume_position = max(0.0, position)

    def next_path(self, current: Path) -> Path:
        """The file that follows ``current`` on this channel's air order."""
        if not self.episodes:
            return current
        if self.tune_in_mode == "broadcast":
            epoch = self._broadcast_epoch if self._broadcast_epoch is not None else time.time()
            self._ensure_broadcast(epoch=epoch)
        if self._broadcast is not None:
            return self._broadcast.next_path(current)
        ordered = list(self.episodes)
        i = _index_of_path(ordered, current)
        if i < 0:
            return ordered[0]
        return ordered[(i + 1) % len(ordered)]

    def guide_filenames(self, playing: Path) -> tuple[str, str]:
        """NOW / NEXT file names for the bottom OSD, from this channel's air order."""
        paths = self.air_paths() or list(self.episodes)
        if not paths:
            return playing.name, playing.name
        i = _index_of_path(paths, playing)
        if i < 0:
            now, nxt = playing, paths[0]
        else:
            now, nxt = paths[i], paths[(i + 1) % len(paths)]
        return now.stem, nxt.stem

    def air_paths(self) -> List[Path]:
        if self.tune_in_mode == "broadcast":
            epoch = self._broadcast_epoch if self._broadcast_epoch is not None else time.time()
            sched = self._ensure_broadcast(epoch=epoch)
            if sched is not None:
                return list(sched._episodes)
        if self.is_empty:
            return []
        return list(self.episodes)

    def ends_cycle(self, current: Path) -> bool:
        paths = self.air_paths()
        return bool(paths) and _index_of_path(paths, current) == len(paths) - 1

    def play_after(self, current: Path) -> Optional[PlayRequest]:
        """Next file after ``current`` (wraps to the first). Used on EOF."""
        if self.is_empty:
            return None
        nxt = self.next_path(current)
        return PlayRequest(path=nxt, start=0.0)

    # -- broadcast schedule -------------------------------------------------
    def _ensure_broadcast(self, *, epoch: float) -> Optional[BroadcastSchedule]:
        if self._broadcast is not None:
            return self._broadcast
        if self.is_empty:
            return None
        use_epoch = self._broadcast_epoch if self._broadcast_epoch is not None else epoch
        ordered = list(self.episodes)
        durations: List[float] = []
        for path in ordered:
            dur = probe_duration(path)
            durations.append(dur if dur else DEFAULT_EPISODE_SECONDS)
        self._broadcast = BroadcastSchedule(
            ordered, durations, epoch=use_epoch, rng=self._rng
        )
        return self._broadcast


class ChannelLineup:
    """An ordered set of channels with remote-style navigation."""

    def __init__(self, channels: Sequence[Channel]) -> None:
        if not channels:
            raise ValueError("a lineup needs at least one channel")
        # Present channels in ascending channel-number order, like a real tuner.
        self._channels: List[Channel] = sorted(channels, key=lambda c: c.number)
        self._by_number: Dict[int, Channel] = {c.number: c for c in self._channels}
        self._index = 0

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self):
        return iter(self._channels)

    @property
    def current(self) -> Channel:
        return self._channels[self._index]

    @property
    def numbers(self) -> List[int]:
        return [c.number for c in self._channels]

    def has_number(self, number: int) -> bool:
        return number in self._by_number

    def index_of(self, number: int) -> Optional[int]:
        for i, ch in enumerate(self._channels):
            if ch.number == number:
                return i
        return None

    def up(self) -> Channel:
        self._index = (self._index + 1) % len(self._channels)
        return self.current

    def down(self) -> Channel:
        self._index = (self._index - 1) % len(self._channels)
        return self.current

    def select_number(self, number: int) -> Optional[Channel]:
        idx = self.index_of(number)
        if idx is None:
            return None
        self._index = idx
        return self.current

    def select_index(self, index: int) -> Channel:
        self._index = index % len(self._channels)
        return self.current


def _folder_show_names(episodes: Sequence[Path], root: Path) -> List[str]:
    """Show folder names (not flat filenames like s1e1) used for channel labels."""
    names: List[str] = []
    try:
        root_r = root.resolve()
    except OSError:
        root_r = root
    for path in episodes:
        try:
            rel = path.resolve().relative_to(root_r)
        except (ValueError, OSError):
            continue
        if len(rel.parts) > 1:
            names.append(rel.parts[0])
    return sorted(set(names), key=str.lower)


def build_lineup(
    config: Config,
    *,
    rng: Optional[random.Random] = None,
    today: Optional[date] = None,
) -> ChannelLineup:
    """Scan configured folders / deal the mixed pool and build the lineup."""
    today = today or date.today()
    deal_rng = rng or random.Random(
        mixed_deal_seed(config.shuffle_seed, today.isoformat())
    )
    broadcast_epoch = (
        start_of_local_day() if config.tune_in == "broadcast" else None
    )

    dedicated_cfgs = [c for c in config.channels if not c.from_pool]
    pool_cfgs = [c for c in config.channels if c.from_pool]

    channels: List[Channel] = []
    dedicated_files: set[Path] = set()

    for i, ch_cfg in enumerate(dedicated_cfgs):
        episodes = scan_episodes(
            ch_cfg.path,
            config.video_extensions,
            recursive=config.scan_recursive,
            exclude=ch_cfg.exclude,
            exclude_seasons=ch_cfg.exclude_seasons,
        )
        for p in episodes:
            try:
                dedicated_files.add(p.resolve())
            except OSError:
                dedicated_files.add(p)
        if not episodes:
            log.warning(
                "channel %s (%s) has no playable episodes in %s",
                ch_cfg.number, ch_cfg.name, ch_cfg.path,
            )
        else:
            episodes = air_order(
                episodes,
                deal_rng,
                root=ch_cfg.path,
                block_size=config.show_block_episodes,
            )
        channels.append(
            _make_channel(
                config, ch_cfg, episodes, index=i, broadcast_epoch=broadcast_epoch
            )
        )

    if pool_cfgs:
        pool_root = config.mixed.path if config.mixed is not None else pool_cfgs[0].path
        pool = [
            p
            for p in scan_episodes(
                pool_root,
                config.video_extensions,
                recursive=config.scan_recursive,
            )
            if _safe_resolve(p) not in dedicated_files
        ]
        mapping = load_show_map(config.state_path)
        numbers = [c.number for c in pool_cfgs]
        dealt = deal_episodes(
            pool,
            len(pool_cfgs),
            deal_rng,
            root=pool_root,
            mapping=mapping,
            channel_numbers=numbers,
        )
        save_show_map(config.state_path, mapping)
        playlists = mixed_playlists(
            pool,
            dealt,
            root=pool_root,
            block_size=config.show_block_episodes,
        )
        for i, (ch_cfg, home_eps, episodes) in enumerate(
            zip(pool_cfgs, dealt, playlists)
        ):
            folder_homes = _folder_show_names(home_eps, pool_root)
            home_keys = {show_key(p, pool_root) for p in home_eps}
            if len(folder_homes) == 1:
                ch_cfg = replace(ch_cfg, name=_prettify_name(folder_homes[0]))
            elif len(folder_homes) > 1 and len(home_keys) == 1:
                ch_cfg = replace(ch_cfg, name=_prettify_name(next(iter(home_keys))))
            if not episodes:
                log.info(
                    "channel %s (%s) has no cartoons in the current library",
                    ch_cfg.number, ch_cfg.name,
                )
            channels.append(
                _make_channel(
                    config,
                    ch_cfg,
                    episodes,
                    index=1000 + i,
                    broadcast_epoch=broadcast_epoch,
                )
            )

    return ChannelLineup(channels)


def _make_channel(
    config: Config,
    ch_cfg,
    episodes: Sequence[Path],
    *,
    index: int,
    broadcast_epoch: Optional[float],
) -> Channel:
    if config.shuffle_seed is not None:
        ch_rng = random.Random(channel_rng_seed(config.shuffle_seed, ch_cfg.number, index))
    else:
        ch_rng = random.Random()
    return Channel(
        ch_cfg,
        episodes,
        tune_in=config.tune_in,
        start_offset_min=config.start_offset_min,
        start_offset_max=config.start_offset_max,
        rng=ch_rng,
        broadcast_epoch=broadcast_epoch,
        show_block_episodes=config.show_block_episodes,
    )


__all__ = [
    "Channel",
    "ChannelLineup",
    "PlayRequest",
    "BroadcastSchedule",
    "scan_episodes",
    "detect_season",
    "deal_episodes",
    "show_key",
    "franchise_key",
    "episode_sort_key",
    "series_prefix",
    "channel_rng_seed",
    "mixed_playlists",
    "rotate_playlist_to_shows",
    "load_show_map",
    "save_show_map",
    "start_of_local_day",
    "build_lineup",
]
