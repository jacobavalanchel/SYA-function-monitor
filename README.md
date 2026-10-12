# Monitor Function

Private Git submodule for SYA local focus monitoring.

## Scope

- Screen monitoring.
- Camera monitoring.
- Intel RealSense camera source for monitor, inference, and training capture.
- RealSense 2D RGB recording with configurable segmenting and disk cleanup.
- Local monitor record storage.
- Monitor test and permission validation hooks.
- Webcam attention detection demo and LLM screen reading prototype.

## Current source anchors

- `app/electron/monitor/screen.ts`
- `app/electron/monitor/camera.ts`
- `app/electron/monitor/storage.ts`
- `app/src/pages/MonitorTestPage.tsx`
- `app/src/types/electron.d.ts`

The Electron app can load monitor through the packaged artifact runtime when
`app/function-runtime/monitor/<platform-arch>` is prepared, and falls back to
the source adapter during local development.

## Function Artifact API

The monitor artifact exposes a local HTTP API documented in
`api.openapi.json`. Electron maps each OpenAPI `operationId` to a Function API
action, so the UI can call:

```ts
await window.sya.functions.invoke('monitor', 'screen.status', {});
await window.sya.functions.invoke('monitor', 'camera.show', {});
await window.sya.functions.invoke('monitor', 'actions.list', {});
```

Discovery and generic invocation endpoints:

| Method | Path | Function action |
| --- | --- | --- |
| `GET` | `/api/monitor/actions` | `actions.list` |
| `GET` | `/api/monitor/capabilities` | `capabilities.list` |
| `POST` | `/api/monitor/invoke` | `invoke` |

Generic invoke request:

```json
{
  "action": "screen.start",
  "payload": {
    "rateMs": 10000
  }
}
```

Operational monitor endpoints:

| Method | Path | Function action |
| --- | --- | --- |
| `GET` | `/health` | `health` |
| `GET` | `/manifest` | `manifest` |
| `GET` | `/api/monitor/screen/status` | `screen.status` |
| `POST` | `/api/monitor/screen/start` | `screen.start` |
| `POST` | `/api/monitor/screen/stop` | `screen.stop` |
| `GET` | `/api/monitor/camera/status` | `camera.status` |
| `POST` | `/api/monitor/camera/start` | `camera.start` |
| `POST` | `/api/monitor/camera/stop` | `camera.stop` |
| `POST` | `/api/monitor/camera/show` | `camera.show` |
| `POST` | `/api/monitor/camera/hide` | `camera.hide` |
| `GET` | `/api/monitor/camera/sources` | `camera.sources` |
| `GET` | `/api/monitor/realsense/status` | `realsense.status` |
| `GET` | `/api/monitor/realsense/devices` | `realsense.devices` |
| `POST` | `/api/monitor/realsense/start` | `realsense.start` |
| `POST` | `/api/monitor/realsense/stop` | `realsense.stop` |
| `POST` | `/api/monitor/realsense/capture` | `realsense.capture` |
| `GET` | `/api/monitor/realsense/frame/latest` | `realsense.frame.latest` |
| `POST` | `/api/monitor/realsense/training/start` | `realsense.training.start` |
| `POST` | `/api/monitor/realsense/training/stop` | `realsense.training.stop` |
| `GET` | `/api/monitor/storage/stats` | `storage.stats` |
| `GET` | `/api/monitor/storage/screen-records` | `storage.screenRecords` |
| `GET` | `/api/monitor/storage/camera-records` | `storage.cameraRecords` |
| `GET` | `/api/monitor/attention/config/status` | `attention.config.status` |
| `GET` | `/api/monitor/events` | `events` |

## Intel RealSense Camera Source

The monitor runtime can expose an Intel RealSense RGB-D camera as a camera data
source for inference and training capture. The adapter is optional: the server
still starts when the RealSense SDK, `pyrealsense2`, or hardware is unavailable,
and `realsense.status` reports the missing dependency or device state.

In the SYA desktop app, `/#/settings` is the user-facing configuration surface
for RealSense. It lets the user choose built-in vs RealSense source, select a
device serial, set save location, segment duration or file-size split policy,
minimum free-space cleanup, codec/container, resolution, FPS, bitrate, and CRF.
The settings page applies an RGB-only recording path: depth is disabled for this
path so SYA records 2D RGB video for monitor, inference, and training source
capture.

### Pinned macOS L515 Runtime

Do not install an arbitrary `pyrealsense2` wheel for the macOS L515 path. The
repository installer builds the reviewed SDK and Python bindings with these
pins:

- librealsense `2.54.2` at commit
  `2dbaaf5964490cb02f811cf2ed38c8a893f5c027`
- pybind11 at commit `0bd8896a4010f2d91b2340570c24fa08606ec406`
- patch SHA-256
  `eb3acdba968cb716133a176a0051af7d06568594cd3b1d8a65f6d3b1202317c1`
- L515 product ID `0b64` and supported firmware `1.5.8.1`
- Python 3.9, 3.10, or 3.11

Install Homebrew `cmake` and `libusb`, then run:

```bash
python3 scripts/realsense_l515.py install
MONITOR_REALSENSE_VENV="$HOME/.sya/monitor/.venv" \
  scripts/realsense-l515 doctor --json
```

The default SDK root is `~/.sya/monitor/realsense` and the default venv is
`~/.sya/monitor/.venv`. The installer verifies the patch checksum, target
Python architecture, exact SDK version, module location, and repeat application
of the source patch. It does not install a firmware image or firmware updater,
and it never elevates itself.

`doctor` can detect the USB device without being able to open it through the
SDK. If it reports `privilegeRequired: true`, stop and review the local macOS
camera/USB access policy. Do not automate `sudo`. The bounded `smoke` command
checks SDK and firmware before creating a streaming pipeline, so an unsupported
version cannot start acquisition.

The arm64 clean-install acceptance path builds twice in the same isolated root,
runs all 43 Python tests in the installed venv, checks the installed CMake
packages, and executes a MediaPipe FaceMesh inference without opening a camera.

### Live Preview and Focus Events

While RealSense is active, the runtime keeps only the latest RGB-D sample in
memory and publishes a bounded JPEG preview at approximately 2 FPS. Consumers
can poll `realsense.frame.latest` or subscribe to SSE events declared by the
manifest:

- `realsense.frame` for preview and depth metadata
- `attention.status` for `focused`, `distracted`, `away`, or `unknown`
- `realsense.training.sample` for persisted training captures

Five consecutive matching observations are required before focus state
changes. Five consecutive no-face observations produce `away`; acquisition or
inference failures fail closed to `unknown` and clear stale preview data.

### MON-018 observation contract (source only)

`attention.status` now emits a fresh record for every completed focus inference,
including unchanged states. Each has a unique UUID-based `id`, `kind`
(`transition`, `observation`, or `boundary`), `timestamp == observedAt` in epoch
milliseconds, `stateChangedAt`, `expectedIntervalMs`, stream `sessionId`, and
`delivery: "live"`. State/source changes update `stateChangedAt`; unchanged
observations retain it. The first observation initializes it. Five-sample
classification debounce still applies; each new sample during debounce is an
observation of the current debounced state, not a fabricated new classification.

The default `focusFps: 2` means an expected 500 ms interval (200 ms at 5 FPS).
The declared interval is `ceil(1000 / min(focusFps, colorFps))`; hardware stalls
are not hidden by repeating cached data. Reading status, cached previews, or
capture metadata never advances observation time. A successful new stream start
creates a fresh UUID session; training sessions are independent. Stops emit an
unknown `boundary: "stopped"`; acquisition or inference failures emit unknown
`boundary: "disconnected"`. No inference result may be published after its
stream is stopped. There is no independent energy model and no `energy` field.

These fields are optional on the legacy `AttentionRecord` schema for old
artifacts, but required by `AttentionObservation` for this provider. Missing
fields mean freshness cannot be inferred. SSE IDs match attention record IDs.
The real and mock runtimes retain the latest **2,048 attention events** in memory;
preview images are never retained for replay. A reconnect with `Last-Event-ID`
replays only records strictly after a recognized retained ID, preserving IDs,
timestamps and sessions with `delivery: "replay"`, before fresh live events.
Absent, unknown, or evicted IDs start live only and do not imply full backfill.
The buffer is lost on runtime restart; durable app history belongs to the host's
SQLite store. Replayed observations must never refresh current state or trigger
energy, and the host's disconnected boundary preserves gaps outside retention.
HTTP authentication and process provenance boundaries are unchanged.

The mock's explicit attention-status action represents one synthetic observation
per call; repeated identical states produce distinct observations. Camera stop
emits a boundary and camera restart begins a new session. It never runs an
attention timer that re-stamps cached results. The TypeScript source adapter
has no RealSense inference implementation and continues reporting unavailable;
it cannot advertise or manufacture observation heartbeats. This source change
has not updated any locked/distributed artifact or been verified on hardware.

List sources and connected devices:

```ts
await window.sya.functions.invoke('monitor', 'camera.sources', {});
await window.sya.functions.invoke('monitor', 'realsense.devices', {});
await window.sya.functions.invoke('monitor', 'realsense.status', {});
```

Start the RealSense stream through the same camera source boundary used by SYA:

```ts
await window.sya.functions.invoke('monitor', 'camera.start', {
  source: 'realsense',
  width: 640,
  height: 480,
  fps: 30,
  colorWidth: 640,
  colorHeight: 480,
  depthWidth: 640,
  depthHeight: 480
});
```

The runtime resolves each enabled stream to the nearest camera-supported
profile. This lets L515 devices use different color and depth resolutions while
preserving the simple `width`/`height` fallback for devices that support one
shared size.

Capture one RGB/depth sample for inference:

```ts
await window.sya.functions.invoke('monitor', 'realsense.capture', {
  purpose: 'inference',
  label: 'focused',
  keepRunning: true
});
```

Create a training data session and save samples under that session:

```ts
await window.sya.functions.invoke('monitor', 'realsense.training.start', {
  label: 'attentive',
  taskId: 'writing-introduction',
  notes: 'Desk setup A'
});

await window.sya.functions.invoke('monitor', 'realsense.capture', {
  purpose: 'training',
  label: 'attentive'
});

await window.sya.functions.invoke('monitor', 'realsense.training.stop', {
  stopCamera: true
});
```

Captured data is written to
`$MONITOR_RECORDS_DIR/realsense/<session>/<sample>/` or, if the environment
variable is not set, `~/.sya/monitor/records/realsense/<session>/<sample>/`.
Each sample contains:

- `color.ppm`: RGB color frame.
- `depth.pgm`: 16-bit depth frame with `depthScaleMeters` in metadata.
- `metadata.json`: source, label, capture time, file paths, dimensions, and
  depth scale.

`config.local.json` can set RealSense defaults:

```json
{
  "camera": {
    "defaultSource": "builtin",
    "realsense": {
      "width": 640,
      "height": 480,
      "fps": 30,
      "colorWidth": 640,
      "colorHeight": 480,
      "colorFps": 30,
      "depthWidth": 640,
      "depthHeight": 480,
      "depthFps": 30,
      "enableColor": true,
      "enableDepth": true,
      "serial": "",
      "recordsDir": ""
    }
  }
}
```

For the RGB-only settings-page path, set `enableDepth` to `false` and keep
`enableColor` as `true`.

### Linux RealSense RGB Recorder

For Linux hosts where the RealSense RGB stream is exposed through V4L2, the
repository includes a standalone recorder at
`function/monitor/scripts/realsense_rgb_recorder.py`.

Example:

```bash
python3 function/monitor/scripts/realsense_rgb_recorder.py \
  --record-dir ~/realsense-rgb-recordings \
  --home-path /home \
  --segment-seconds 3600 \
  --min-free-gb 20
```

The recorder:

- scans `/dev/video*` for Intel RealSense RGB/YUYV devices,
- records RGB video with `ffmpeg`,
- writes one `.mp4` file per segment, one hour by default,
- monitors remaining space under `/home` by default,
- deletes the oldest completed `.mp4` segment when free space drops below the
  threshold,
- records no depth stream and no audio.

Required host tools:

- `ffmpeg`
- `v4l2-ctl`
- `udevadm`

Useful environment variables:

- `REALSENSE_RGB_DEVICE`
- `REALSENSE_RGB_RECORD_DIR`
- `REALSENSE_RGB_HOME_PATH`
- `REALSENSE_RGB_MIN_FREE_GB`
- `REALSENSE_RGB_SEGMENT_SECONDS`
- `REALSENSE_RGB_WIDTH`
- `REALSENSE_RGB_HEIGHT`
- `REALSENSE_RGB_FPS`
- `REALSENSE_RGB_X264_CRF`

## Mock Server

The tracked mock lives in `mock/` and can be used before executable UI E2E
runs. It exposes deterministic Camera, Screen, and Attention state without
requiring screen capture, camera hardware, or macOS permissions.

```bash
cd function/monitor/mock
PORT=8770 python3 server.py
python3 attention_control.py --base-url http://127.0.0.1:8770
```

`attention_control.py` lets a tester press keys in a shell window to change the
mock `attentionStatus` output in real time. The mock server persists that value
in later status API responses and emits `attention.status` over SSE to connected
UI clients. See `mock/README.md` for the full state contract and keymap.

## Webcam Attention Demo

Attention-aware browser demo with webcam-based face tracking and optional
screen reading. It uses MediaPipe face tracking to detect attention, captures a
screen region around the mouse cursor, and can send the screen context through
an OpenAI-compatible LLM endpoint.

### Quick Start

```bash
cd function/monitor
python3 server.py
```

Open `http://localhost:8765` in Chrome.

If running the server on a remote machine, forward the port first:

```bash
ssh -L 8765:localhost:8765 <host>
```

### Features

- Attention detection through webcam face and gaze tracking.
- Screen capture through `getDisplayMedia`.
- Mouse region preview with a live cropped square around the cursor.
- LLM screen reasoning after 3 seconds of mouse dwell.

### LLM API

The demo can call the HKUST-GZ AIGC API or another OpenAI-compatible endpoint.

- Default endpoint: `https://aigc-api.hkust-gz.edu.cn/v1/chat/completions`
- Default model: `gpt-4-turbo`
- Auth: bearer token loaded by `server.py` from local config

Create local API config before enabling screen reading:

```bash
cd function/monitor
cp config.example.json config.local.json
```

Then edit `config.local.json`:

```json
{
  "api": {
    "path": "https://aigc-api.hkust-gz.edu.cn/v1/chat/completions",
    "key": "YOUR_API_KEY",
    "model": "gpt-4-turbo"
  }
}
```

`config.local.json` and `config.json` are ignored by Git. Do not commit API
keys. To use a custom config location, set `MONITOR_CONFIG_PATH` before running
the server:

```bash
MONITOR_CONFIG_PATH=/secure/path/monitor-api.json python3 server.py
```

Requests are proxied through `server.py` at `/api/llm` to avoid browser CORS
restrictions. The browser never receives the API key.

### Files

| File | Purpose |
| --- | --- |
| `index.html` | Self-contained frontend app |
| `server.py` | Static file server and LLM API proxy |
| `config.example.json` | Example API config schema |
| `test_vision_support.py` | Script to test which models support vision |
| `HANDOVER.md` | Original demo handover notes |
| `module.json` | Parent module metadata |
| `package.json` | Private package metadata |

### Requirements

- Python 3
- Chrome browser with webcam access
- Internet access for MediaPipe CDN/model loading
- LLM endpoint access only if screen reading is enabled
