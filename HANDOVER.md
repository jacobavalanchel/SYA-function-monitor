# Webcam Attention Demo — Handover

Current location in this repository: `function/monitor/`.

This file was originally written when the demo lived in a
`webcam-attention-demo/` directory. The directory layer has since been removed,
and the demo files now live directly under the monitor function module.

## What This Is

A single-page browser app that replicates the attention detection logic from
`attention-aware-video-player` entirely in the browser (no Python backend needed).

- Opens the user's laptop webcam via `getUserMedia`
- Runs MediaPipe FaceLandmarker (JS Tasks API) in-browser on CPU
- Applies the same landmark-ratio attention logic as `run_headless.py`
- Outputs PLAY / PAUSE state in a banner and controls a loaded video file
- Optionally captures the user's screen via `getDisplayMedia`

## Files

```
webcam-attention-demo/
  index.html    — the entire app (self-contained, no build step)
  HANDOVER.md   — this file
```

## How to Serve

```bash
cd function/monitor
python3 -m http.server 8899
```

Then on the user's laptop, SSH port-forward and open in browser:

```bash
ssh -L 8899:localhost:8899 AIO-HPC
# open http://localhost:8899
```

Direct IP also works if the port is reachable: `http://10.120.18.240:8899`

The page must be served over HTTP (not opened as a `file://` URL) because
`getUserMedia` and `getDisplayMedia` require a secure context. `localhost`
qualifies; a raw `file://` path does not.

## Attention Logic

Ported directly from:
- `../related-work-search/attention-aware-video-player/run_headless.py`
- `../related-work-search/attention-aware-video-player/repo/helpers.py`

Landmark indices and thresholds are identical to the Python version.

### Thresholds (hardcoded, from `attention_thresholds.json`)

| Key | Value |
|---|---|
| FACE_VERTICAL_DOWN | 0.66 |
| FACE_VERTICAL_UP | 0.46 |
| FACE_HORIZONTAL_RIGHT | 0.77 |
| FACE_HORIZONTAL_LEFT | 0.34 |
| EYE_VERTICAL_DOWN | 0.49 |
| EYE_VERTICAL_UP | 0.34 |
| EYE_HORIZONTAL_RIGHT | 0.66 |
| EYE_HORIZONTAL_LEFT | 0.35 |

These are the default thresholds from the original repo. They are not
per-user calibrated. If detection feels too sensitive or too loose, edit
the `T` object near the top of the `<script>` block in `index.html`.

### Attention Rule

Attentive if any of:
- All four directions are Center
- Face Up + Eye Down (compensating gaze)
- Face Down + Eye Up
- Face Left + Eye Right
- Face Right + Eye Left

State is confirmed only after 5 consecutive frames agree (same as Python
`check_attention_state` with `deque(maxlen=5)`).

## External Dependencies (CDN, loaded at runtime)

| Resource | URL |
|---|---|
| MediaPipe Tasks Vision JS | `cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14` |
| FaceLandmarker WASM | same CDN, `/wasm` path |
| FaceLandmarker model | `storage.googleapis.com/mediapipe-models/face_landmarker/...` |

Both CDN and model storage require outbound internet access from the user's
browser (not from the server). The HPC only serves `index.html`; all model
loading happens client-side.

If the user's browser cannot reach Google Storage (e.g. mainland China
network), the model download will fail. Fix: download the model file to the
server and serve it locally:

```bash
# on the HPC
curl -L -o /hpc2hdd/home/yhan603/SYA/webcam-attention-demo/face_landmarker.task \
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
```

Then in `index.html`, change `modelAssetPath` to `"/face_landmarker.task"`.
A copy already exists at:
`/hpc2hdd/home/yhan603/SYA/related-work-search/assessment-of-cognitive-load-and-emotion/face_landmarker.task`

## Known Limitations

- **Thresholds are not calibrated per user.** The original app has a
  calibration step (look at screen edges). This page uses fixed defaults.
  Add a calibration UI if per-user accuracy matters.
- **Screen capture play/pause**: `getDisplayMedia` gives a live stream —
  there is no play/pause handle. The attention state is shown in the banner
  but does not pause the captured screen. Only a loaded local video file
  is controlled automatically.
- **Single face only.** `numFaces: 1` — multi-user not supported.
- **CPU inference only.** `delegate: "CPU"` is set explicitly to avoid GPU
  contention. On a modern laptop this runs comfortably at webcam frame rate.

## Related Files

| Path | Purpose |
|---|---|
| `../related-work-search/attention-aware-video-player/run_headless.py` | Python headless version (video file input, stdout output) |
| `../related-work-search/attention-aware-video-player/repo/` | Original repo (webcam + WebSocket + tkinter GUI) |
| `../related-work-search/attention-aware-video-player/repo/attention_thresholds.json` | Default thresholds source |
| `../related-work-search/assessment-of-cognitive-load-and-emotion/face_landmarker.task` | FaceLandmarker model file |
| `../related-work-search/assessment-of-cognitive-load-and-emotion/conda-env-py310/` | Python env for running the headless version |

## Possible Next Steps

- Add in-page threshold calibration (mirror the original `calibrate_thresholds.py` flow)
- Export per-session attention timeline as CSV
- Connect to a WebSocket server to send play/pause to an external player
  (replicating the original browser extension approach)
- Integrate with ROAMM or COLET data pipeline for offline annotation
