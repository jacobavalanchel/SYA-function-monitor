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

### Publisher runtime digests

The producer can hash a staged, unpacked runtime with the same canonical format
as the desktop consumer. Use Node.js 22.17.1 or newer; no additional package is
required. Finish staging before hashing and keep the directory unchanged during
the operation.

```bash
node scripts/hash-runtime.mjs /path/to/staged-runtime
node scripts/hash-runtime.mjs /path/to/staged-runtime --json
node scripts/hash-runtime.mjs /path/to/staged-runtime --archive /path/to/runtime.zip
node tests/test_release_digest.mjs
```

The default output is the unpacked tree's hexadecimal SHA-256. `--json` emits
`{"schemaVersion":1,"algorithm":"sya-runtime-tree-sha256-v1","runtimeSha256":"…"}`.
`--archive` implies JSON and adds `sha256`, the SHA-256 of the supplied archive's
bytes. Output goes only to stdout; the tool does not modify either input. Keep
redirected output outside the staged directory. The publisher must separately
ensure the archive actually contains that staged tree.

`runtimeTreeSha256` is copied verbatim from SYA-UI's
`scripts/hash-function-runtime.mjs` at commit
`6c3366eed46e4af5751b90ae0706fe45dc4586d7`. The format walks directories recursively
in NFC-normalized UTF-8 byte order and hashes fields as
`label + NUL + decimal byte length + NUL + bytes`: `directory` for each directory
path, then `file` and `content` for each file. Paths use `/` separators and NFC;
empty directories participate. Modes and modification times do not participate.
Only a regular root-level `artifact-provenance.json` is excluded, allowing
consumer provenance to be added after verification. Producers must not include
that file in the staged or archived runtime. The publisher API and all CLI modes
reject its presence; only the shared canonical hash function applies the exclusion. Symbolic
links, unsupported entries, and normalization-equivalent sibling names fail.
The implementation uses Node's built-in
[SHA-256 API](https://nodejs.org/download/release/v22.17.1/docs/api/crypto.html#cryptocreatehashalgorithm-options)
and Unicode's established
[NFC normalization](https://www.unicode.org/reports/tr15/), without a new hashing
framework. Tests pin a fixed vector and compare the function and output with the
parent consumer when this checkout is inside SYA-UI; that parity test is explicitly
skipped in a standalone checkout.

These values describe the supplied local bytes. They do not establish publisher
identity, source revision, release approval, or authenticity. A trusted publishing
workflow must bind the digests to its release metadata and provenance; generating
them locally does not create that trust.

### 已暂存服务的认证验证

宿主为每次启动提供独立 `FUNCTION_RUNTIME_TOKEN` 与
`FUNCTION_RUNTIME_PROOF_KEY`。真实服务和 mock 只对已经通过 token 认证的
`GET /health` 返回 `X-SYA-Runtime-Proof`，值为
`HMAC-SHA256(key, "sya-runtime-v1")` 的小写十六进制。
未提供 proof key 时不返回此 header；无 token 的 standalone 保留原行为，
但不能作为已认证宿主 runtime 的证据。mock attention_control.py 同样从该环境变量读取 token，
仅向回环 HTTP 发送，禁用代理和重定向；未配置 token 时保留原 standalone 行为。所有声明 GET/POST/SSE 操作在 token
模式下拒绝缺失或错误 token。health 的响应头已记录在 OpenAPI。

```bash
node scripts/test-staged-runtime.mjs /path/to/staged-runtime
node tests/test_staged_runtime.mjs
node tests/test_release_archives.mjs
```

此工具在 POSIX runner 启动 manifest 指定的 launcher，使用临时 HOME、空配置、
独立 records 目录和随机端口，不继承发布或模型凭据。成功请求只覆盖 health、
manifest、actions、screen status、latest frame 和立即取消的 SSE 握手；
不调用会读取设备状态或启动采集的认证操作。验证结束或失败均有界清理进程，
运行目录 digest 必须保持不变；动作发现须与 OpenAPI 对齐，状态/预览检查必需字段。
workflow 将 smoke 输出保存于 stage 外，所有归档均解包核对同一已验证目录摘要，
之后才生成外部双摘要元数据。它不验证硬件、模型结果或跨平台 binary；
当前 wrapper 依赖外部 Python 的限制仍然存在。

### 发布源码与现有 tag 绑定

发布 workflow 在构建前和发布前各运行一次
`scripts/verify-release-source.mjs`。它只读取 Git refs 与 GitHub release 列表，
要求正常 `X.Y.Z` 版本与 manifest、package、OpenAPI 一致；已有 `vX.Y.Z`
tag 的本地与远程对象必须一致，其解析后的 commit 必须等于检出的 HEAD 与
`GITHUB_SHA`，且该 commit 必须属于当前远端 `main` 历史。`workflow_dispatch` 不会根据
输入偷偷切换源码；选中的 branch/tag 必须正好指向待发布 commit。
缺失 tag、未合并源码、工作区已修改、tag 移动或读取远端失败均中止。

两次检查之间保存的绑定必须不变。`source-provenance.json` 位于 release asset
目录，随 archive、双摘要文件一起写入 `SHA256SUMS.txt`，不注入 runtime 目录。
其字段是 `schemaVersion`、`repository`、`tag`、`version`、`sourceSha`、
`tagObjectSha` 和 `trustedBranch`。这是发布流程声明的来源信息，不是独立签名
或 GitHub attestation；消费者仍须从可信 release 下载并独立校验 archive/tree。

发布前使用分页 release API 检查所有已发布项和 draft；同 tag 已存在时拒绝
继续。发布步骤使用 GitHub CLI 的 create-only 命令并启用 `--verify-tag`，
不再使用默认可更新同 tag release 的 action。GitHub CLI 对携带附件的 create
先创建新 draft、上传附件，再发布；失败不进入覆盖既有资产的路径。
参考 [GitHub CLI create](https://cli.github.com/manual/gh_release_create)、
[Release API](https://docs.github.com/en/rest/releases/releases) 和
[workflow_dispatch commit 语义](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)。

每个 tag 的 workflow 串行执行，但检查与远端发布之间仍可能发生外部 ref 修改。
仓库的 tag protection / immutable-release 设置负责约束这个剩余窗口及发布后的
修改；本地测试未查询或配置这些远端策略，因此不宣称永久不可变。已有 release
或 draft 不会自动删除、编辑或重用；需要人工核查并选择新的版本。

```bash
node tests/test_release_source.mjs
```

测试使用临时本地 Git 仓库和模拟的只读 GitHub CLI 响应，覆盖 annotated tag、
分支与事件 SHA 错配、main ancestry、同 commit tag 对象重写、已有 draft、
网络错误和发布 shell 的拒绝传播；不创建真实 tag/release，不调用远端发布。

普通 PR 使用独立的只读 CI。CI 与发布共用 `scripts/stage-runtime.sh` 和
`scripts/archive-runtime.sh`：前者原子占用新输出目录，拒绝输出嵌入输入目录，
只复制 Git 管理的 scripts/patches 与三个 adapter 入口的编译文件；后者沿用已通过
smoke 的摘要，对四种归档逐项解包复算。PR 不生成虚假的 tag 来源记录或上传资产。
最终发布还要求精确的资产集合和完整、无重复的校验清单，上传仅使用验证后的列表。

在模块根目录可运行非发布检查（目标 `release/stage` 与 `release/out` 须尚不存在）：

```bash
npm ci --ignore-scripts
npm run build
bash scripts/stage-runtime.sh
node scripts/test-staged-runtime.mjs release/stage > release/smoke-result.json
bash scripts/archive-runtime.sh 0.2.0
```

该配方仍为依赖外部 Python 的本地候选包，不证明四个平台原生运行或正式发布完成。

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

### FOC-011 eye observations (internal, source only)

The RealSense source now reuses the same fresh-frame FaceMesh result to measure
left/right eye aspect ratio (EAR) and eye width in image-pixel coordinates.
`eye_observation.py` retains only scalar observations in a session-owned memory
window: 60 seconds, at most 600 samples, with a maximum adjacent-frame gap of
`2 / min(focusFps, colorFps)` seconds. These are operational bounds, not calibrated
energy thresholds. Attention classification, sampling rates, public HTTP/SSE
payloads and the prohibition on `energy` remain unchanged.

Actual color-frame identity and host acquisition monotonic time determine
ordering; inference completion time is separate, and acquisition UTC is metadata.
Host acquisition time is not sensor exposure time. Missing/repeated/out-of-order
identities, invalid geometry and excessive gaps break continuity. Stops, failures
and new sessions clear previous samples and continuity; an inference failure may
then be retained as an invalid observation. Same-session resets preserve ordering
watermarks. An old worker cannot append to or clear a newer session. Snapshot
coverage uses only proven adjacent valid intervals, clips them
to the configured duration and never extends beyond the last observation. One
older anchor may be retained within the sample limit but is excluded from sample
counts and EAR distributions. Capacity truncation is reported explicitly.
Snapshot endpoints must not move backward within a session; requests for pruned
history fail explicitly. An incoherent acquisition/completion clock pair cannot
advance retention time or the offline evaluator's default report endpoint.

EAR is uncalibrated geometry. Coverage describes sampling continuity, not image
quality, eye closure, single-person presence or identity continuity.
`max_num_faces=1` does not prove that only one person is present, and this API has
no per-eye quality confidence. No images or landmarks are stored by this window,
and this change adds no collection, upload, model dependency or public capability.
`extractor_version` identifies the fixed landmark-contour/EAR algorithm. It does
not attest to the installed MediaPipe version or an injected processor's identity;
actual processor, model and authorized-data provenance still need separate evidence.

The offline evaluator accepts explicit schema-1 JSONL and a versioned
configuration. Run from the Monitor repository or staged package root with Python
3.11 or newer; the staged CLI loads its pure module from `assets/`:

```sh
python3.11 scripts/evaluate-eye-observations.py \
  --config CONFIG.json --input OBSERVATIONS.jsonl --as-of-monotonic-s 100.5
```

`CONFIG.json`:

```json
{"schema_version":1,"extractor_version":"mediapipe-face-mesh-0.10.21-ear-v1","session_id":"synthetic","duration_seconds":2,"max_samples":20,"max_gap_seconds":1}
```

`OBSERVATIONS.jsonl` (two deliberately synthetic samples):

```jsonl
{"schema_version":1,"session_id":"synthetic","color_frame_number":1,"captured_at_ms":100000,"acquired_monotonic_s":100,"completed_monotonic_s":100.05,"geometry":{"schema_version":1,"extractor_version":"mediapipe-face-mesh-0.10.21-ear-v1","width":640,"height":480,"left_ear":0.2,"right_ear":0.25,"left_width_px":40,"right_width_px":40,"reason":null}}
{"schema_version":1,"session_id":"synthetic","color_frame_number":2,"captured_at_ms":100500,"acquired_monotonic_s":100.5,"completed_monotonic_s":100.55,"geometry":{"schema_version":1,"extractor_version":"mediapipe-face-mesh-0.10.21-ear-v1","width":640,"height":480,"left_ear":0.2,"right_ear":0.25,"left_width_px":40,"right_width_px":40,"reason":null}}
```

The report has `valid_samples: 2`, `coverage_seconds: 0.5`,
`coverage_ratio: 0.25` and `valid_sample_rate_hz: 1`. Both ratio/rate denominators
are the configured two-second window; they are not a model confidence or a claim
that the short trace sustained one frame per second. The report also includes
invalid reason counts, interval/latency/EAR distributions and capacity flags.
`reason_counts` records each rejected sample's first failing validation, so it
does not enumerate every issue present in that sample. `interval_seconds`
contains only proven adjacent valid intervals intersecting the window; rejected
observations and continuity gaps are excluded, so it is not an exhaustive
acquisition-cadence distribution.
Configuration is bounded to 16 KiB, each JSONL line to 64 KiB and offline capacity
to 100,000 samples. Unknown fields/versions, invalid numbers and malformed input
fail with a nonzero exit and only a fixed `{line,code}` error on stderr; no partial
success report is written. Finite but unusable observations instead contribute
quality rejection reasons without resetting the configured session.

Synthetic geometry, stream-lifecycle, offline-input and packaging tests validate
this source foundation. Real model accuracy, authorized-data provenance and
evaluation, complete acquisition-cadence and image-quality evaluation, actual
camera performance and packaged production behavior remain unverified; FOC-011
remains Partial. No production artifact lock is changed.

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

## 本机自包含候选与第三方依赖

macOS arm64 的本机原型入口、完整依赖锁、合成诊断与发布边界见 [packaging/README.md](packaging/README.md)。该候选不替换生产 artifact，也不证明硬件、签名或最低系统版本已通过。Browser demo 与 Python FaceMesh 使用不同模型资产，不能互相替代验收。

### 第三方依赖与许可证

原型采用 PyInstaller（GPLv2+，含商业 bundle 例外）、MediaPipe/librealsense（Apache-2.0）、NumPy（BSD）、Pillow（MIT-CMU）、libusb（LGPL-2.1）及完整锁定的传递依赖；证书库使用 certifi（MPL-2.0）。具体固定版本、官方来源和已知分发问题见 [依赖说明](packaging/dependency-notes.md)。正式分发前仍须归档完整 LICENSE/NOTICE 及 libusb 对应来源/适用材料，本页不作为许可证合规完成证明。
