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
| `GET` | `/api/monitor/storage/stats` | `storage.stats` |
| `GET` | `/api/monitor/storage/screen-records` | `storage.screenRecords` |
| `GET` | `/api/monitor/storage/camera-records` | `storage.cameraRecords` |
| `GET` | `/api/monitor/attention/config/status` | `attention.config.status` |
| `GET` | `/api/monitor/events` | `events` |

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
