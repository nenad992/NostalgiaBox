# Mixed 10-channel schedule (Pi + Tilt)

**Date:** 2026-09-01  
**Status:** accepted

Same YAML drives Raspberry Pi (`config.yaml`) and Tilt (`config.tilt.yaml`). Paths differ; behaviour does not.

## Behaviour

- **10 mixed channels** from one folder (`mixed.path`).
- Shuffle **shows** (subfolders like `Stitch/` / `Pokemon/`, or the series name in the filename). Deal shows round-robin onto mixed channels. Episodes of one show stay in filename order on that channel (no last-then-first). The next day (or new seed) deals shows again.
- Files already used by **dedicated** `channels:` entries are removed from the pool first.
- Mixed slots with no file: colour bars + persistent OSD **«Ovaj kanal nema danas crtaća»**.
- **`tune_in: broadcast`**, **`start_offset: 0`**. Shared **start-of-local-day** epoch so flipping away and back joins the clock, not a random intro skip.
- Dedicated folder channels stay optional and keep their own numbers (use 11+ if mixed is 1–10).

## Config (sketch)

```yaml
tune_in: broadcast
start_offset: 0
empty_channel_message: "Ovaj kanal nema danas crtaća"

mixed:
  path: /media/nostalgiabox/pool   # Tilt: dev/media/Sample Channel
  count: 10
  first_number: 1
  name_prefix: Kanal

# optional dedicated shows
# channels:
#   - number: 11
#     name: Arthur
#     path: /media/nostalgiabox/arthur
```

Existing `media_root` / `channels:`-only configs still work (old folder-per-channel).
