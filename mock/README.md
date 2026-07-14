# Monitor Mock Server

Standalone mock for the SYA Monitor function API. It does not require screen
capture, camera hardware, macOS permissions, or the Electron app.

The mock is meant to be tracked in git and used before UI executable E2E runs.
It exposes deterministic state, mutating actions, SSE events, and a command-line
tool for manually changing Attention status in real time.

## Start

```bash
cd function/monitor/mock
PORT=8770 python3 server.py
```

Health check:

```bash
curl http://127.0.0.1:8770/health
```

## Attention Status Command Tool

Start the mock server in one terminal, then run:

```bash
cd function/monitor/mock
python3 attention_control.py --base-url http://127.0.0.1:8770
```

Keymap:

| Key | attentionStatus | confidence |
|-----|-----------------|------------|
| `f` | `focused` | `0.95` |
| `d` | `distracted` | `0.35` |
| `a` | `away` | `0.10` |
| `u` | `unknown` | `0.00` |
| `h` | print help | n/a |
| `q` | quit | n/a |

Each keypress posts to:

```text
POST /api/monitor/mock/attention/status
```

The server updates subsequent `attention.config.status` responses and emits:

```text
event: attention.status
event: attention.config.status
```

This lets the UI app receive Attention status changes exactly as a user would
trigger them from a shell window during executable E2E or manual testing.

## State Contract

Camera status:

```bash
curl http://127.0.0.1:8770/api/monitor/camera/status
```

Fields:

- `isActive`
- `windowCreated`
- `faceDetected`
- `lastFrameTime`
- `storagePath`

Mutating actions:

```bash
curl -X POST http://127.0.0.1:8770/api/monitor/camera/start
curl -X POST http://127.0.0.1:8770/api/monitor/camera/show
curl -X POST http://127.0.0.1:8770/api/monitor/camera/hide
curl -X POST http://127.0.0.1:8770/api/monitor/camera/stop
```

`camera.start/stop/show/hide` must change later `camera.status` responses.
Periodic `camera.frame` SSE events also update `faceDetected` and
`lastFrameTime`.

Required next-step/acceptance fix:

- After each `camera.frame` SSE payload, later `camera.status` calls must return
  the same latest `faceDetected` value and `lastFrameTime`.

Screen status:

```bash
curl http://127.0.0.1:8770/api/monitor/screen/status
```

Fields:

- `isMonitoring`
- `captureRateMs`
- `activeWindow`
- `idleTimeSeconds`
- `lastCaptureTime`
- `storagePath`

Mutating actions:

```bash
curl -X POST http://127.0.0.1:8770/api/monitor/screen/start \
  -H 'Content-Type: application/json' \
  -d '{"rateMs":5000}'
curl -X POST http://127.0.0.1:8770/api/monitor/screen/stop
```

`screen.start/stop` must change later `screen.status` responses. Periodic
`screen.capture` SSE events also update `activeWindow`, `idleTimeSeconds`, and
`lastCaptureTime`.

Attention status:

```bash
curl http://127.0.0.1:8770/api/monitor/attention/config/status
curl -X POST http://127.0.0.1:8770/api/monitor/mock/attention/status \
  -H 'Content-Type: application/json' \
  -d '{"attentionStatus":"distracted","confidence":0.35,"source":"manual"}'
```

Fields:

- `configured`
- `apiPath`
- `model`
- `configFile`
- `attentionStatus`
- `confidence`
- `source`
- `reason`
- `lastUpdatedAt`

Allowed `attentionStatus` values are `focused`, `distracted`, `away`, and
`unknown`.

## SSE

```bash
curl -N http://127.0.0.1:8770/api/monitor/events
```

The mock server uses a threaded HTTP server so this open SSE connection must not
block regular API requests.

Required next-step/acceptance fix:

- The server must remain a concurrent HTTP server. Keeping `/api/monitor/events`
  open must never block `GET /api/monitor/*/status` or mutating `POST` calls.

Events:

- `ready`
- `camera.frame`
- `screen.capture`
- `screen.status`
- `camera.status`
- `attention.status`
- `attention.config.status`

## Generic Invoke

The mock mirrors the real function bridge's generic invoke route:

```bash
curl -X POST http://127.0.0.1:8770/api/monitor/invoke \
  -H 'Content-Type: application/json' \
  -d '{"action":"screen.status"}'
```

Mock-only attention update through invoke:

```bash
curl -X POST http://127.0.0.1:8770/api/monitor/invoke \
  -H 'Content-Type: application/json' \
  -d '{"action":"attention.status.set","payload":{"attentionStatus":"focused"}}'
```

## Quick Verification

```bash
python3 -m py_compile server.py attention_control.py
PORT=8772 python3 server.py
curl http://127.0.0.1:8772/api/monitor/attention/config/status
curl -X POST http://127.0.0.1:8772/api/monitor/mock/attention/status \
  -H 'Content-Type: application/json' \
  -d '{"attentionStatus":"focused","confidence":0.95}'
```
