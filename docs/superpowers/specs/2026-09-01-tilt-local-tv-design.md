# Local Tilt “mini TV” for NostalgiaBox

**Date:** 2026-09-01  
**Status:** proposed  
**Goal:** Develop on a Mac and *see* the product: video, CRT look, channel banner, volume bar, channel changes.

This is not a Raspberry Pi deploy. HDMI, Flirc, CEC, and systemd stay on the Pi. Tilt runs the same Python app on the laptop with an mpv window as the screen.

## Problem

`--dry-run` uses a mock player (logs only, no picture). Tilt’s log pane is not a TTY, so stdin remote control does not work there. macOS has no `evdev`, so the Pi keyboard/remote backend does not work either.

We need a host-side loop: real `MpvPlayer` + a way to send the same `Action`s the remote would send, without a terminal.

## Approach (chosen)

Tilt on the Mac starts:

1. **`test`** — `pytest` when Python/tests change.
2. **`tv`** — `python -m nostalgiabox` with a Tilt config (real mpv, not `--dry-run`).
3. **UI buttons** in Tilt — CH+, CH−, Vol+, Vol−, Mute, Info, Power, Quit — via `curl` to a localhost-only HTTP control port.

Not chosen: Docker/noVNC (worse video, more moving parts); SSH deploy to a Pi (needs hardware).

## Architecture

```
  Tilt UI  --POST /channel/up-->  DevHttpBackend (127.0.0.1)
                                        |
                                        v
  InputManager.put(InputEvent) --> TVApp  --> MpvPlayer (window on Mac)
                                        ^
  pytest (separate Tilt resource)       |
  config.tilt.yaml + MEDIA_ROOT --------+
```

The HTTP server is another **input backend**. It only enqueues `InputEvent`s. `TVApp` does not learn about HTTP. Channel scan, shuffle, overlays, and CRT shader stay unchanged.

## Components

### 1. `DevHttpBackend` (`nostalgiabox/input/dev_http.py`)

- Bind **127.0.0.1 only** (never 0.0.0.0).
- Default port **8765** (override in config).
- `POST` routes map 1:1 to existing actions:

  | Path | Action |
  |------|--------|
  | `/channel/up` | `CHANNEL_UP` |
  | `/channel/down` | `CHANNEL_DOWN` |
  | `/volume/up` | `VOLUME_UP` |
  | `/volume/down` | `VOLUME_DOWN` |
  | `/mute` | `MUTE` |
  | `/info` | `INFO` |
  | `/last` | `LAST_CHANNEL` |
  | `/power` | `POWER` |
  | `/quit` | `QUIT` |
  | `/digit/{0-9}` | `DIGIT` |

- `GET /health` → 200 when the backend is listening.
- Unknown paths → 404. Non-POST (except health) → 405.
- No auth (loopback only). stdlib `http.server` in a daemon thread is enough.
- `create_backends` starts it when `input.dev_http` is true (or `port` is set). Off by default so the Pi is unchanged.

### 2. Windowed player

Today `MpvPlayer` is constructed with `fullscreen=True` and no config knob.

- Add `fullscreen: bool` on `Config` (default **true**, Pi behaviour unchanged).
- `config.tilt.yaml` sets `fullscreen: false` so the “TV” is an mpv window beside Tilt.
- Pass that flag into `MpvPlayer` from `TVApp.from_config`.

### 3. Tilt config and media

Committed **`config.tilt.yaml`**:

- `media_root` from env `MEDIA_ROOT`, default `./dev/media` (absolute path resolved in the Tiltfile or a tiny wrapper).
- `input.dev_http: true` (and port 8765).
- `input.keyboard: false`, `cec: false`, `stdin: false`.
- `power_off_on_min_volume: false` (must not `poweroff` the Mac).
- `transition: none` so local setup does not require generated static/glitch clips.
- CRT and UI left on so the look is visible.

**`dev/media/`** is gitignored except a `README` (or `.gitkeep` + docs): one folder per channel, real `.mp4`/`.mkv` files. Empty dummy files are not enough to see picture.

### 4. `Tiltfile`

- No Kubernetes, no Docker.
- `local_resource('test', ...)`: `.venv/bin/pytest`, deps `nostalgiabox/`, `tests/`.
- `local_resource('tv', serve_cmd=...)`: `.venv/bin/python -m nostalgiabox --config config.tilt.yaml`, deps on the app + that config.
- `serve_env`: `MEDIA_ROOT` if we generate/patch config; otherwise the YAML uses a relative `dev/media`.
- Tilt `uibutton` (or `cmd_button`) resources: `curl -sf -X POST http://127.0.0.1:8765/...`
- `tv` resource_deps: venv/install if we add a one-shot `pip install -e ".[dev,local]"` resource.

### 5. Python extras

- New optional extra **`local`**: `python-mpv` only (not `evdev`).
- Document: `brew install mpv` so libmpv is on the Mac; `pip install -e ".[dev,local]"`.

## Data flow

1. Developer puts shows in `dev/media/Show Name/*.mp4`.
2. `tilt up` → pytest runs; `tv` starts; mpv window appears; Tilt shows buttons.
3. Button → HTTP POST → `InputEvent` on the existing queue → same handlers as the remote.
4. Code change under `nostalgiabox/` restarts `tv` and re-runs tests (auto).

## Error handling

- Missing `python-mpv` / libmpv: existing `RuntimeError` from `MpvPlayer`; Tilt resource shows red; README troubleshooting (`brew install mpv`, reinstall extra).
- Empty `media_root`: app already warns and shows no-signal behaviour; `--check` remains available as an optional Tilt resource (manual trigger) — nice-to-have, not required for v1.
- Port in use: fail startup with a clear log (do not silently pick another port).
- `curl` before the server is up: Tilt button fails until `/health` is ready (optional readiness_probe on `tv`).

## Testing

- Unit tests for route → `InputEvent` mapping (in-process handler, no real bind required if we extract a pure `path_to_event` function).
- One test that `create_backends({"dev_http": True, "keyboard": False, "cec": False})` can bind 127.0.0.1 on an ephemeral port and `GET /health` succeeds; tear down the backend.
- Existing `tests/` must still pass without libmpv.
- No requirement to automate mpv GUI in CI.

## Out of scope

- Deploying to a Pi, Flirc, HDMI-CEC, systemd, Docker, browser-embedded video.
- Changing shuffle/tune-in/OSD behaviour except wiring `fullscreen` and the new input backend.
- Bundling copyrighted sample episodes.

## How to use (after implementation)

```bash
brew install mpv tilt
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,local]"
# copy real episodes into dev/media/<channel name>/
tilt up
```

Watch the mpv window; use Tilt buttons to change channels. Quit via the Quit button or stopping the `tv` resource.
