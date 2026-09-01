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
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import AbstractSet, Dict, List, Optional, Sequence

# Patterns for pulling a season number out of a file/folder path.
_SEASON_PATTERNS = (
    re.compile(r"s(\d{1,2})[ ._-]?e\d{1,3}", re.IGNORECASE),   # S06E01, s6e1
    re.compile(r"\bseason[ ._-]*(\d{1,2})\b", re.IGNORECASE),  # Season 6
    re.compile(r"\b(\d{1,2})x\d{1,3}\b"),                       # 6x01
)

from .config import ChannelConfig, Config
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
    kept broadcasting while nobody was watching. The running order is a single
    shuffle that loops forever.
    """

    def __init__(
        self,
        episodes: Sequence[Path],
        durations: Sequence[float],
        *,
        epoch: float,
        rng: random.Random,
    ) -> None:
        if len(episodes) != len(durations):
            raise ValueError("episodes and durations must be the same length")
        order = list(range(len(episodes)))
        rng.shuffle(order)
        self._episodes = [episodes[i] for i in order]
        self._durations = [max(1.0, float(durations[i])) for i in order]
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


def deal_episodes(
    episodes: Sequence[Path],
    n_channels: int,
    rng: random.Random,
) -> List[List[Path]]:
    """Shuffle ``episodes`` and deal round-robin so each file hits one channel.

    Extra channels beyond the library size get an empty list. After this deal
    is exhausted (next day / new seed), shuffle again so files can move.
    """
    if n_channels < 1:
        raise ValueError("n_channels must be >= 1")
    items = list(episodes)
    rng.shuffle(items)
    buckets: List[List[Path]] = [[] for _ in range(n_channels)]
    for i, episode in enumerate(items):
        buckets[i % n_channels].append(episode)
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

    # -- broadcast schedule -------------------------------------------------
    def _ensure_broadcast(self, *, epoch: float) -> Optional[BroadcastSchedule]:
        if self._broadcast is not None:
            return self._broadcast
        if self.is_empty:
            return None
        use_epoch = self._broadcast_epoch if self._broadcast_epoch is not None else epoch
        durations: List[float] = []
        for path in self.episodes:
            dur = probe_duration(path)
            durations.append(dur if dur else DEFAULT_EPISODE_SECONDS)
        self._broadcast = BroadcastSchedule(
            self.episodes, durations, epoch=use_epoch, rng=self._rng
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
        dealt = deal_episodes(pool, len(pool_cfgs), deal_rng)
        for i, (ch_cfg, episodes) in enumerate(zip(pool_cfgs, dealt)):
            if not episodes:
                log.info(
                    "channel %s (%s) has no cartoons in today's deal",
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
    )


__all__ = [
    "Channel",
    "ChannelLineup",
    "PlayRequest",
    "BroadcastSchedule",
    "scan_episodes",
    "detect_season",
    "deal_episodes",
    "mixed_deal_seed",
    "start_of_local_day",
    "build_lineup",
]
