import random

from nostalgiabox.channel import (
    BroadcastSchedule,
    Channel,
    ChannelLineup,
    build_lineup,
    detect_season,
    scan_episodes,
)
from nostalgiabox.config import config_from_dict
from tests.helpers import make_show


def _channel(tmp_path, name="arthur", episodes=4, **kw):
    folder = make_show(tmp_path, name, episodes)
    from nostalgiabox.config import ChannelConfig

    cfg = ChannelConfig(number=kw.pop("number", 3), name=name, path=folder)
    eps = scan_episodes(folder, [".mp4"])
    return Channel(cfg, eps, rng=random.Random(0), **kw)


def test_scan_episodes_sorted_and_filtered(tmp_path):
    folder = make_show(tmp_path, "arthur", 3)
    (folder / "notes.txt").write_text("nope")
    (folder / ".DS_Store").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"])
    assert [p.name for p in eps] == [
        "arthur_ep01.mp4",
        "arthur_ep02.mp4",
        "arthur_ep03.mp4",
    ]


def test_detect_season():
    assert detect_season("Arthur S06E01.mp4") == 6
    assert detect_season("arthur.s6e12.mkv") == 6
    assert detect_season("Season 12/ep03.mp4") == 12
    assert detect_season("Arthur 6x05.mp4") == 6
    assert detect_season("Arthurs Perfect Christmas.mp4") is None


def test_scan_exclude_globs(tmp_path):
    folder = tmp_path / "arthur"
    (folder / "Season 1").mkdir(parents=True)
    (folder / "Specials").mkdir(parents=True)
    (folder / "Season 1" / "S01E01.mp4").write_bytes(b"")
    (folder / "Specials" / "Arthur Special.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude=["*special*"])
    names = [p.name for p in eps]
    assert names == ["S01E01.mp4"]


def test_scan_exclude_seasons(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    for s in (1, 5, 6, 7, 25):
        (folder / f"Arthur S{s:02d}E01.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude_seasons=set(range(6, 26)))
    seasons = sorted(detect_season(p.name) for p in eps)
    assert seasons == [1, 5]  # 6..25 removed


def test_build_lineup_applies_channel_excludes(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    (folder / "Arthur S01E01.mp4").write_bytes(b"")
    (folder / "Arthur S06E01.mp4").write_bytes(b"")
    (folder / "Arthur Special.mp4").write_bytes(b"")
    cfg = config_from_dict(
        {
            "channels": [
                {
                    "number": 3,
                    "name": "Arthur",
                    "path": str(folder),
                    "exclude": ["*special*"],
                    "exclude_seasons": ["6-25"],
                }
            ]
        }
    )
    lineup = build_lineup(cfg)
    eps = list(lineup)[0].episodes
    assert [p.name for p in eps] == ["Arthur S01E01.mp4"]


def test_scan_recursive(tmp_path):
    base = tmp_path / "show"
    (base / "season1").mkdir(parents=True)
    (base / "season2").mkdir(parents=True)
    (base / "season1" / "a.mp4").write_bytes(b"")
    (base / "season2" / "b.mp4").write_bytes(b"")
    assert len(scan_episodes(base, [".mp4"], recursive=True)) == 2
    assert len(scan_episodes(base, [".mp4"], recursive=False)) == 0


def test_tune_in_random_plays_from_start(tmp_path):
    ch = _channel(tmp_path, tune_in="random")
    req = ch.tune_in()
    assert req is not None
    assert req.start == 0.0
    assert req.path in ch.episodes


def test_advance_continues_shuffle(tmp_path):
    ch = _channel(tmp_path, episodes=4, tune_in="random")
    seen = {ch.tune_in().path}
    for _ in range(3):
        seen.add(ch.advance().path)
    assert len(seen) == 4  # every episode shown before repeats


def test_start_offset_fixed(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=5.0, start_offset_max=5.0)
    assert ch.tune_in().start == 5.0
    assert ch.advance().start == 5.0


def test_start_offset_range(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=6.0, start_offset_max=10.0)
    starts = [ch.tune_in().start for _ in range(20)] + [ch.advance().start for _ in range(20)]
    assert all(6.0 <= s <= 10.0 for s in starts)
    assert len(set(round(s, 3) for s in starts)) > 1  # actually varies


def test_resume_mode_remembers_position(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    first = ch.tune_in()
    ch.remember(first.path, 123.5)
    again = ch.tune_in()
    assert again.path == first.path
    assert again.start == 123.5


def test_empty_channel_returns_none(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    from nostalgiabox.config import ChannelConfig

    ch = Channel(ChannelConfig(number=9, name="Empty", path=folder), [])
    assert ch.is_empty
    assert ch.tune_in() is None
    assert ch.advance() is None


def test_broadcast_schedule_positions():
    from pathlib import Path

    eps = [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
    durs = [100.0, 200.0, 300.0]
    sched = BroadcastSchedule(eps, durs, epoch=0.0, rng=random.Random(0))
    # At t=0 we are at the start of the first item in air order.
    first = sched.at(0.0)
    assert first.path == eps[0]
    assert first.start == 0.0
    # The schedule is a loop of total length 600s; t=600 == t=0.
    assert sched.at(600.0).path == first.path
    # 50s into the cycle we should still be within the first item, offset 50.
    assert sched.at(50.0).start == 50.0


def test_broadcast_tune_in_uses_real_time(tmp_path, monkeypatch):
    # Force probe_duration to a known value so we don't need ffprobe/real media.
    import nostalgiabox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    ch = _channel(tmp_path, episodes=3, tune_in="broadcast")
    # Two tune-ins at different times should generally land at different offsets.
    r1 = ch.tune_in(now=0.0)
    r2 = ch.tune_in(now=30.0)
    assert r1.start == 0.0
    assert r2.start == 30.0


def test_lineup_navigation(tmp_path):
    for n in ("a", "b", "c"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 2, "name": "A", "path": str(tmp_path / "a")},
                {"number": 4, "name": "B", "path": str(tmp_path / "b")},
                {"number": 7, "name": "C", "path": str(tmp_path / "c")},
            ],
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [2, 4, 7]
    assert lineup.current.number == 2
    assert lineup.up().number == 4
    assert lineup.up().number == 7
    assert lineup.up().number == 2  # wraps
    assert lineup.down().number == 7  # wraps back
    assert lineup.select_number(4).number == 4
    assert lineup.select_number(99) is None
    assert lineup.has_number(7)


def test_deal_episodes_unique_until_pool_exhausted():
    from pathlib import Path

    from nostalgiabox.channel import deal_episodes

    eps = [Path(f"v{i}.mp4") for i in range(4)]
    buckets = deal_episodes(eps, 10, random.Random(0))
    assert len(buckets) == 10
    assigned = [p for bucket in buckets for p in bucket]
    assert len(assigned) == 4
    assert len(set(assigned)) == 4
    assert sum(1 for b in buckets if not b) == 6


def test_deal_round_robin_spreads_overflow():
    from pathlib import Path

    from nostalgiabox.channel import deal_episodes

    eps = [Path(f"v{i}.mp4") for i in range(25)]
    buckets = deal_episodes(eps, 10, random.Random(1))
    sizes = [len(b) for b in buckets]
    assert all(s >= 1 for s in sizes)
    flat = [p for b in buckets for p in b]
    assert len(set(flat)) == 25


def test_sticky_show_keeps_channel_when_library_grows(tmp_path):
    from nostalgiabox.channel import deal_episodes

    stitch = tmp_path / "Stitch"
    stitch.mkdir()
    (stitch / "s01e01.mp4").write_bytes(b"x")
    mapping: dict = {}
    first = deal_episodes(
        list(stitch.iterdir()),
        10,
        random.Random(0),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    stitch_ch = next(i for i, b in enumerate(first) if b)
    (stitch / "s01e02.mp4").write_bytes(b"x")
    poke = tmp_path / "Pokemon"
    poke.mkdir()
    (poke / "s01e01.mp4").write_bytes(b"x")
    files = list(stitch.iterdir()) + list(poke.iterdir())
    second = deal_episodes(
        files,
        10,
        random.Random(99),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    assert [p.name for p in second[stitch_ch]] == ["s01e01.mp4", "s01e02.mp4"]
    poke_ch = next(i for i, b in enumerate(second) if b and b[0].parent.name == "Pokemon")
    assert poke_ch != stitch_ch


def test_lineup_sticky_across_rebuild(tmp_path):
    pool = tmp_path / "pool"
    (pool / "Stitch").mkdir(parents=True)
    (pool / "Stitch" / "s01e01.mp4").write_bytes(b"x")
    state = tmp_path / "map.json"
    data = {
        "mixed": {"path": str(pool), "count": 10, "first_number": 1},
        "state_path": str(state),
    }
    a = build_lineup(config_from_dict(data))
    stitch_a = next(c for c in a if c.episodes)
    assert stitch_a.name.lower() == "stitch"
    (pool / "Pokemon").mkdir()
    (pool / "Pokemon" / "s01e01.mp4").write_bytes(b"x")
    b = build_lineup(config_from_dict(data))
    stitch_b = next(c for c in b if c.number == stitch_a.number)
    assert stitch_b.name.lower() == "stitch"
    assert any(p.parent.name == "Stitch" for p in stitch_b.episodes)
    poke = next(c for c in b if c.name.lower() == "pokemon")
    assert poke.number != stitch_a.number
    assert not poke.is_empty
    assert stitch_b.number == stitch_a.number


def test_pack_extra_shows_when_all_channels_full(tmp_path):
    from nostalgiabox.channel import deal_episodes

    files = []
    for i in range(12):
        folder = tmp_path / f"Show{i:02d}"
        folder.mkdir()
        p = folder / "e01.mp4"
        p.write_bytes(b"x")
        files.append(p)
    mapping: dict = {}
    buckets = deal_episodes(
        files,
        10,
        random.Random(0),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    flat = [p for b in buckets for p in b]
    assert len(flat) == 12
    assert len(set(flat)) == 12
    assert len(mapping) == 12


def test_air_order_max_three_then_other_show(tmp_path):
    from nostalgiabox.channel import air_order

    stitch = tmp_path / "Stitch"
    poke = tmp_path / "Pokemon"
    stitch.mkdir()
    poke.mkdir()
    for i in range(1, 6):
        (stitch / f"s01e{i:02d}.mp4").write_bytes(b"x")
        (poke / f"s01e{i:02d}.mp4").write_bytes(b"x")
    eps = list(stitch.iterdir()) + list(poke.iterdir())
    order = air_order(eps, random.Random(0), root=tmp_path, block_size=3)
    assert len(order) == 10
    shows = [p.parent.name for p in order]
    run = 1
    max_run = 1
    for a, b in zip(shows, shows[1:]):
        if a == b:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
    assert max_run <= 3
    # Pokemon is first alphabetically: 3 poke, 3 stitch, remaining poke, remaining stitch
    assert shows[:3] == ["Pokemon", "Pokemon", "Pokemon"]
    assert shows[3:6] == ["Stitch", "Stitch", "Stitch"]
    assert "s01e04.mp4" in {p.name for p in order[6:] if p.parent.name == "Pokemon"}
    rotated = air_order(
        eps, random.Random(0), root=tmp_path, block_size=3, start_key_offset=1
    )
    assert [p.parent.name for p in rotated[:3]] == ["Stitch", "Stitch", "Stitch"]


def test_mixed_playlists_keep_every_channel_busy(tmp_path):
    from nostalgiabox.channel import deal_episodes, mixed_playlists

    stitch = tmp_path / "Stitch"
    poke = tmp_path / "Pokemon"
    stitch.mkdir()
    poke.mkdir()
    for i in range(1, 6):
        (stitch / f"s01e{i:02d}.mp4").write_bytes(b"x")
    for i in range(1, 4):
        (poke / f"s01e{i:02d}.mp4").write_bytes(b"x")
    pool = list(stitch.iterdir()) + list(poke.iterdir())
    homes = deal_episodes(pool, 10, random.Random(0), root=tmp_path)
    lists = mixed_playlists(pool, homes, root=tmp_path, block_size=3)
    stitch_n = sum(1 for pl in lists for p in pl if p.parent.name == "Stitch")
    poke_n = sum(1 for pl in lists for p in pl if p.parent.name == "Pokemon")
    assert stitch_n == 5
    assert poke_n == 3
    assert sum(1 for pl in lists if pl) >= 2
    owned = [p.resolve() for pl in lists for p in pl]
    assert len(owned) == len(set(owned)) == 8


def test_mixed_playlists_stagger_when_one_folder_of_files(tmp_path):
    from nostalgiabox.channel import deal_episodes, mixed_playlists

    pool = tmp_path / "Sample"
    pool.mkdir()
    files = []
    for i in range(4):
        p = pool / f"clip{i}.mp4"
        p.write_bytes(b"x")
        files.append(p)
    homes = deal_episodes(files, 10, random.Random(0), root=pool)
    lists = mixed_playlists(files, homes, root=pool, block_size=3)
    firsts = [pl[0].name for pl in lists if pl]
    assert len(firsts) == 4
    assert len(set(firsts)) == 4


def test_channel_wraps_to_first_episode_not_empty(tmp_path):
    from nostalgiabox.config import ChannelConfig

    folder = tmp_path / "Poke"
    folder.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (folder / name).write_bytes(b"x")
    eps = scan_episodes(folder, [".mp4"])
    ch = Channel(ChannelConfig(number=2, name="P", path=folder), eps, tune_in="broadcast")
    last = eps[-1]
    assert ch.ends_cycle(last)
    nxt = ch.play_after(last)
    assert nxt is not None
    assert nxt.path == eps[0]


def test_mixed_pool_lineup(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir()
    for i in range(4):
        (pool / f"ep{i}.mp4").write_bytes(b"x")
    arthur = make_show(tmp_path, "arthur", 2)
    cfg = config_from_dict(
        {
            "shuffle_seed": 7,
            "mixed": {"path": str(pool), "count": 10, "first_number": 1},
            "channels": [
                {"number": 11, "name": "Arthur", "path": str(arthur)},
            ],
        }
    )
    lineup = build_lineup(cfg, rng=random.Random(0), today=__import__("datetime").date(2026, 9, 1))
    assert len(lineup) == 11
    mixed = [c for c in lineup if c.number <= 10]
    dedicated = [c for c in lineup if c.number == 11]
    assert len(mixed) == 10
    assert len(dedicated[0].episodes) == 2
    mixed_files = [p.name for c in mixed for p in c.episodes]
    assert len(set(mixed_files)) == 4
    assert len(mixed_files) == 4  # each file on one channel only
    assert sum(1 for c in mixed if not c.is_empty) == 4


def test_same_day_deal_is_stable(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir()
    for i in range(8):
        (pool / f"ep{i}.mp4").write_bytes(b"x")
    data = {"shuffle_seed": 3, "mixed": {"path": str(pool), "count": 10}}
    day = __import__("datetime").date(2026, 1, 15)
    a = build_lineup(config_from_dict(data), today=day)
    b = build_lineup(config_from_dict(data), today=day)
    names_a = [[p.name for p in c.episodes] for c in a]
    names_b = [[p.name for p in c.episodes] for c in b]
    assert names_a == names_b


def test_lineup_sorted_by_number(tmp_path):
    for n in ("a", "b"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "channels": [
                {"number": 9, "name": "Nine", "path": str(tmp_path / "a")},
                {"number": 3, "name": "Three", "path": str(tmp_path / "b")},
            ]
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [3, 9]


def test_scan_sxxexx_orders_e2_before_e10(tmp_path):
    from nostalgiabox.channel import scan_episodes

    folder = tmp_path / "show"
    folder.mkdir()
    for name in ("s1e1.mp4", "s1e10.mp4", "s1e2.mp4"):
        (folder / name).write_bytes(b"x")
    names = [p.name for p in scan_episodes(folder, [".mp4"])]
    assert names == ["s1e1.mp4", "s1e2.mp4", "s1e10.mp4"]


def test_franchise_key_groups_sequels():
    from nostalgiabox.channel import franchise_key, show_key
    from pathlib import Path

    assert franchise_key("Inside Out (2015) - Sinhronizovano") == franchise_key(
        "Inside Out 2 (2024) - Sinhronizovano"
    )
    assert franchise_key("Kung Fu Panda (2008) - Sinhronizovano") == franchise_key(
        "Kung Fu Panda 2 (2011) - Sinhronizovano"
    )
    assert franchise_key("Bambi") == franchise_key("Bambi2")
    root = Path("/media/kucniadmin/KINGSTON")
    a = show_key(root / "Inside Out (2015) - Sinhronizovano" / "a.mp4", root)
    b = show_key(root / "Inside Out 2 (2024) - Sinhronizovano" / "b.mp4", root)
    assert a == b == "inside out"


def test_deal_puts_sequels_on_same_home_channel(tmp_path):
    from nostalgiabox.channel import deal_episodes

    io1 = tmp_path / "Inside Out (2015) - Sinhronizovano"
    io2 = tmp_path / "Inside Out 2 (2024) - Sinhronizovano"
    io1.mkdir()
    io2.mkdir()
    (io1 / "m.mp4").write_bytes(b"x")
    (io2 / "m.mp4").write_bytes(b"x")
    poke = tmp_path / "Pokemon"
    poke.mkdir()
    (poke / "s01e01.mp4").write_bytes(b"x")
    mapping: dict = {}
    buckets = deal_episodes(
        [io1 / "m.mp4", io2 / "m.mp4", poke / "s01e01.mp4"],
        10,
        random.Random(0),
        root=tmp_path,
        mapping=mapping,
        channel_numbers=list(range(1, 11)),
    )
    io_ch = next(i for i, b in enumerate(buckets) if any("Inside Out" in str(p) for p in b))
    names = {p.parent.name for p in buckets[io_ch]}
    assert "Inside Out (2015) - Sinhronizovano" in names
    assert "Inside Out 2 (2024) - Sinhronizovano" in names
    poke_ch = next(i for i, b in enumerate(buckets) if b and "Pokemon" in str(b[0]))
    assert poke_ch != io_ch


def test_series_prefix_groups_episodes():
    from nostalgiabox.channel import series_prefix, show_key
    from pathlib import Path

    assert series_prefix("Pokemon S01E02") == "Pokemon"
    assert series_prefix("stitch_ep03") == "stitch"
    assert series_prefix("s1e1") == "s1e1"
    root = Path("/media/pool")
    assert show_key(root / "Stitch" / "s01e09.mp4", root) == "stitch"
    assert show_key(root / "Pokemon" / "s01e01.mp4", root) == "pokemon"


def test_deal_keeps_show_episodes_in_order(tmp_path):
    from nostalgiabox.channel import deal_episodes

    stitch = tmp_path / "Stitch"
    poke = tmp_path / "Pokemon"
    stitch.mkdir()
    poke.mkdir()
    for name in ("s01e10.mp4", "s01e01.mp4", "s01e02.mp4"):
        (stitch / name).write_bytes(b"x")
    (poke / "s01e01.mp4").write_bytes(b"x")
    eps = list(stitch.iterdir()) + list(poke.iterdir())
    buckets = deal_episodes(eps, 10, random.Random(0), root=tmp_path)
    stitch_bucket = next(b for b in buckets if b and b[0].parent.name == "Stitch")
    assert [p.name for p in stitch_bucket] == ["s01e01.mp4", "s01e02.mp4", "s01e10.mp4"]


def test_broadcast_plays_show_in_file_order(tmp_path, monkeypatch):
    import nostalgiabox.channel as channel_mod
    from nostalgiabox.config import ChannelConfig

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 10.0)
    folder = tmp_path / "Stitch"
    folder.mkdir()
    for name in ("s01e02.mp4", "s01e01.mp4"):
        (folder / name).write_bytes(b"x")
    cfg = ChannelConfig(number=1, name="K", path=folder)
    ch = Channel(
        cfg,
        scan_episodes(folder, [".mp4"]),
        tune_in="broadcast",
        rng=random.Random(0),
        broadcast_epoch=0.0,
    )
    r1 = ch.tune_in(now=0.0)
    r2 = ch.tune_in(now=10.0)
    assert r1.path.name == "s01e01.mp4"
    assert r2.path.name == "s01e02.mp4"
    assert ch.next_path(r1.path).name == "s01e02.mp4"
    now, nxt = ch.guide_filenames(r1.path)
    assert now == "s01e01"
    assert nxt == "s01e02"


def test_guide_filenames_follows_this_channel_not_pool_start(tmp_path, monkeypatch):
    import nostalgiabox.channel as channel_mod
    from pathlib import Path

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    pool = tmp_path / "Sample Channel"
    pool.mkdir()
    for i in range(1, 5):
        (pool / f"s1e{i}.mp4").write_bytes(b"x")
    lineup = build_lineup(
        config_from_dict(
            {
                "mixed": {"path": str(pool), "count": 10, "first_number": 1},
                "state_path": str(tmp_path / "map.json"),
                "tune_in": "broadcast",
                "start_offset": 0,
            }
        )
    )
    ch = lineup.select_number(2)
    playing = ch.tune_in(now=ch._broadcast_epoch or 0.0).path
    now, nxt = ch.guide_filenames(Path(playing.name))  # name-only Path
    assert now == playing.stem
    order = [p.stem for p in ch.episodes]
    i = order.index(playing.stem)
    assert nxt == order[(i + 1) % len(order)]


def test_flat_mixed_pool_keeps_kanal_channel_names(tmp_path):
    pool = tmp_path / "Sample Channel"
    pool.mkdir()
    for i in range(1, 5):
        (pool / f"s1e{i}.mp4").write_bytes(b"x")
    lineup = build_lineup(
        config_from_dict(
            {
                "mixed": {"path": str(pool), "count": 10, "first_number": 1},
                "state_path": str(tmp_path / "map.json"),
            }
        )
    )
    names = [c.name for c in lineup]
    assert all(n.startswith("Kanal") for n in names)
    assert "S1E1" not in names


def test_mix_slice_target_is_a_short_block():
    from nostalgiabox.channel import mix_slice_target

    assert mix_slice_target(197, 10) == 3
    assert mix_slice_target(400, 10) == 3
    assert mix_slice_target(8, 10) == 3


def test_long_show_is_sliced_across_channels_in_order(tmp_path):
    from nostalgiabox.channel import deal_episodes

    show = tmp_path / "Lilo"
    show.mkdir()
    files = []
    for i in range(1, 46):
        p = show / f"s01e{i:02d}.mp4"
        p.write_bytes(b"x")
        files.append(p)
    buckets = deal_episodes(
        files, 10, random.Random(0), root=tmp_path, channel_numbers=list(range(1, 11))
    )
    nonempty = [b for b in buckets if b]
    assert len(nonempty) == 10
    sizes = [len(b) for b in nonempty]
    assert min(sizes) >= 3
    assert max(sizes) - min(sizes) <= 3
    owned = [p.resolve() for b in buckets for p in b]
    assert len(owned) == len(set(owned)) == 45
    for bucket in nonempty:
        idxs = [int(p.stem.replace("s01e", "")) for p in bucket]
        assert idxs == sorted(idxs)


def test_sliced_channels_stay_near_even_with_one_fat_show(tmp_path):
    from nostalgiabox.channel import deal_episodes

    files = []
    fat = tmp_path / "Looney"
    fat.mkdir()
    for i in range(97):
        p = fat / f"e{i:03d}.mp4"
        p.write_bytes(b"x")
        files.append(p)
    for n in range(10):
        folder = tmp_path / f"Alpha{n:02d}"
        folder.mkdir()
        p = folder / "film.mp4"
        p.write_bytes(b"x")
        files.append(p)
    buckets = deal_episodes(
        files, 10, random.Random(0), root=tmp_path, channel_numbers=list(range(1, 11))
    )
    sizes = [len(b) for b in buckets]
    assert min(sizes) >= 1
    assert max(sizes) - min(sizes) <= 3
    assert sum(sizes) == 107
    owned = [p.resolve() for b in buckets for p in b]
    assert len(set(owned)) == 107
    for bucket in buckets:
        parents = {p.parent.name for p in bucket}
        assert "Looney" in parents
    assert any(
        any(p.parent.name.startswith("Alpha") for p in bucket) for bucket in buckets
    )


def test_every_channel_mixes_series_and_movies(tmp_path):
    from nostalgiabox.channel import deal_episodes, mixed_playlists, show_key

    files = []
    for show, n in (("Lilo", 30), ("Looney", 30)):
        folder = tmp_path / show
        folder.mkdir()
        for i in range(1, n + 1):
            p = folder / f"s01e{i:02d}.mp4"
            p.write_bytes(b"x")
            files.append(p)
    for n in range(10):
        folder = tmp_path / f"Alpha{n:02d}"
        folder.mkdir()
        p = folder / "film.mp4"
        p.write_bytes(b"x")
        files.append(p)
    buckets = deal_episodes(
        files, 10, random.Random(0), root=tmp_path, channel_numbers=list(range(1, 11))
    )
    lists = mixed_playlists(files, buckets, root=tmp_path, block_size=3)
    for i, (bucket, playlist) in enumerate(zip(buckets, lists)):
        keys = {show_key(p, tmp_path) for p in bucket}
        assert "lilo" in keys
        assert "looney" in keys
        assert any(k.startswith("alpha") for k in keys)
        shows = [show_key(p, tmp_path) for p in playlist]
        run = 1
        max_run = 1
        for a, b in zip(shows, shows[1:]):
            if a == b:
                run += 1
                max_run = max(max_run, run)
            else:
                run = 1
        assert max_run <= 3
        if i:
            assert shows[0] != [show_key(p, tmp_path) for p in lists[0]][0]
    owned = [p.resolve() for pl in lists for p in pl]
    assert len(set(owned)) == 70

