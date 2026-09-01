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

# Patterns for pulling a season number out of a file/folder path.
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
    if not root.exists():
        log.warning("channel folder does not exist: %s", root)
        return []
    exts = {e.lower() for e in extensions}
    patterns = [p.lower() for p in exclude]
    walker = root.rglob("*") if recursive else root.glob("*")
    episodes = [
        p
        for p in walker
        if p.is_file()
        and p.suffix.lower() in exts
        and not p.name.startswith(".")
        and not _is_excluded(p, root, patterns, exclude_seasons)
    ]
    episodes.sort(key=lambda p: str(p).lower())
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
        try:
            i = self._episodes.index(current)
        except ValueError:
            return self._episodes[0]
        return self._episodes[(i + 1) % len(self._episodes)]


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
                    return top.lower()
            return series_prefix(path.stem).lower()
        except ValueError:
            pass
    parent = path.parent.name
    if parent and parent not in (".", "") and detect_season(parent) is None:
        if not parent.lower().startswith("season"):
            return parent.lower()
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
        groups[key] = sorted(groups[key], key=lambda p: str(p).lower())
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


def mixed_deal_seed(shuffle_seed: Optional[int], day_iso: str) -> int:
    """Stable 31-bit seed so the same day + config yields the same deal."""
    material = f"{shuffle_seed!s}|{day_iso}|mixed-deal".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % (2**31)


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
        self._bag: Optional[ShuffleBag[Path]] = (
            ShuffleBag(self.episodes, self._rng) if self.episodes else None
        )
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
    def _next_shuffled(self) -> PlayRequest:
        assert self._bag is not None
        if self.start_offset_max > self.start_offset_min:
            start = self._rng.uniform(self.start_offset_min, self.start_offset_max)
        else:
            start = self.start_offset_min
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
        ordered = air_order(
            self.episodes,
            random.Random(0),
            root=self.config.path,
            block_size=self._block_size,
        )
        i = ordered.index(current) if current in ordered else -1
        if i < 0:
            return ordered[0]
        return ordered[(i + 1) % len(ordered)]

    def air_paths(self) -> List[Path]:
        if self.tune_in_mode == "broadcast":
            epoch = self._broadcast_epoch if self._broadcast_epoch is not None else time.time()
            sched = self._ensure_broadcast(epoch=epoch)
            if sched is not None:
                return list(sched._episodes)
        if self.is_empty:
            return []
        return air_order(
            self.episodes, self._rng, root=self.config.path, block_size=self._block_size
        )

    def ends_cycle(self, current: Path) -> bool:
        paths = self.air_paths()
        return bool(paths) and current == paths[-1]

    def play_after(self, current: Path) -> Optional[PlayRequest]:
        """Next file after ``current`` (wraps to the first). Used on EOF."""
        if self.is_empty:
            return None
        nxt = self.next_path(current)
        if self.tune_in_mode == "broadcast":
            return PlayRequest(path=nxt, start=0.0)
        if self.start_offset_max > self.start_offset_min:
            start = self._rng.uniform(self.start_offset_min, self.start_offset_max)
        else:
            start = self.start_offset_min
        return PlayRequest(path=nxt, start=start)

    # -- broadcast schedule -------------------------------------------------
    def _ensure_broadcast(self, *, epoch: float) -> Optional[BroadcastSchedule]:
        if self._broadcast is not None:
            return self._broadcast
        if self.is_empty:
            return None
        use_epoch = self._broadcast_epoch if self._broadcast_epoch is not None else epoch
        ordered = air_order(
            self.episodes, self._rng, root=self.config.path, block_size=self._block_size
        )
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
        dedicated_files.update(p.resolve() for p in episodes)
        if not episodes:
            log.warning(
                "channel %s (%s) has no playable episodes in %s",
                ch_cfg.number, ch_cfg.name, ch_cfg.path,
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
            if p.resolve() not in dedicated_files
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
        shows_on: Dict[int, List[str]] = {}
        for key, number in mapping.items():
            shows_on.setdefault(number, []).append(key)
        for i, (ch_cfg, episodes) in enumerate(zip(pool_cfgs, dealt)):
            names = sorted(shows_on.get(ch_cfg.number, []))
            if len(names) == 1:
                ch_cfg = replace(ch_cfg, name=_prettify_name(names[0]))
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
        ch_rng = random.Random(
            hash((config.shuffle_seed, ch_cfg.number, index)) & 0xFFFFFFFF
        )
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
    "series_prefix",
    "air_order",
    "load_show_map",
    "save_show_map",
    "start_of_local_day",
    "build_lineup",
]
