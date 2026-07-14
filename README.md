# Monitor Function

Private Git submodule for SYA local focus monitoring.

## Scope

- Screen monitoring.
- Camera monitoring.
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

Install the Intel RealSense SDK and Python bindings in the environment that
runs `server.py`:

```bash
python3 -m pip install pyrealsense2
```

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
