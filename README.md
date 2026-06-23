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

Runtime code is still wired through the main app. Move implementation here once
the private submodule repository is created and published.

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
- Default model in the page: `gpt-4-turbo`
- Auth: bearer token supplied in the page at runtime

Do not commit API keys. The API key input is intentionally blank by default.
Requests are proxied through `server.py` at `/api/llm` to avoid browser CORS
restrictions.

### Files

| File | Purpose |
| --- | --- |
| `index.html` | Self-contained frontend app |
| `server.py` | Static file server and LLM API proxy |
| `test_vision_support.py` | Script to test which models support vision |
| `HANDOVER.md` | Original demo handover notes |
| `module.json` | Parent module metadata |
| `package.json` | Private package metadata |

### Requirements

- Python 3
- Chrome browser with webcam access
- Internet access for MediaPipe CDN/model loading
- LLM endpoint access only if screen reading is enabled
