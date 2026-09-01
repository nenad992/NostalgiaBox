# Now/next OSD and ordered show schedule

**Date:** 2026-09-01  
**Status:** approved  
**Goal:** Bottom NOW/NEXT filenames on channel change (4s, replace on fast zap). Mix shows onto channels, but play each show’s episodes in file order — not last-then-first.

## NOW / NEXT

- Bottom of the picture: `NOW  <filename stem>` and `NEXT  <filename stem>`.
- Same lifetime as the channel bug (`channel_bug_seconds`, default 4).
- Same overlay slot: a new zap **replaces** the text and **restarts** the timer (no stacked leftovers).

## Schedule

- Group files by subfolder under the pool, else by series name in the filename (`Pokemon S01E02` → Pokemon).
- Shuffle **shows** (daily seed) onto sticky homes, then every mixed channel airs the full pool (rotated to start at the home show). A show stays on one home channel.
- Within a show, episodes stay in natural filename order. At most `show_block_episodes` (default 3) in a row, then another show.
- Broadcast airs that list in order and loops. No per-file shuffle inside a show.
