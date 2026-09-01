# HDMI idle pause (keep broadcast clock, stop decoding)

**Date:** 2026-09-01  
**Status:** approved  
**Goal:** When the TV is off (or this HDMI link is down), stop actually playing files after 10 minutes. When the TV comes back, retune the **last selected channel** to whatever the **broadcast clock** says is on now. Do not decode 200 episodes in the dark.

## Behaviour

- Detect **HDMI link**, not remote buttons: Linux DRM connector `status` (`connected` / `disconnected`) on HDMI outputs.
- Unknown (no sysfs, Mac/Tilt): treat as connected. Never idle-pause.
- Any HDMI connector `connected` → signal present. HDMI connectors exist but all `disconnected` → signal absent.
- Signal absent for **`hdmi_idle_pause_seconds`** (default **600**, `0` = off): `player.stop()`, clear overlays, stop advancing episodes. Channel selection is kept.
- Signal returns: `tune_current(show_static=False)` on the current channel. Broadcast mode uses wall-clock `tune_in()` so you join mid-whatever-would-be-airing. No overnight decode.
- Channel changes while idle update the lineup only; picture stays stopped until HDMI returns.
- Tilt: `hdmi_idle_pause_seconds: 0`.

## Limits

Wrong TV input while the Pi still sees HPD `connected` will keep playing. CEC power status is out of scope for this pass.

## Config

```yaml
hdmi_idle_pause_seconds: 600
```
