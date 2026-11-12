"""
SYA Debugger & Observer Web UI.
FastAPI web application with WebSocket log streaming, live RealSense camera feed,
screen preview, and interactive module input/output inspection.
"""

import os
import secrets
import sys
import time
import json
import asyncio
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from .app import SYASoftware


def create_app(
    sya_app: Optional[SYASoftware] = None,
    work_scope: Optional[str] = None,
    current_goal: Optional[str] = None,
    window_size: float = 6.0,
    step_size: float = 10.0,
    device_index: int = 0
) -> FastAPI:
    app = FastAPI(title="SYA Debugger & Observer")
    token = os.environ.get("SYA_DESKTOP_ENGINE_TOKEN")
    software = sya_app or SYASoftware()

    if token:
        @app.middleware("http")
        async def require_desktop_owner(request: Request, call_next):
            if not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
                return JSONResponse({"status": "unauthorized"}, status_code=401)
            return await call_next(request)

        software.camera_master_active = False
        software.screen_master_active = False
        software.accessibility_master_active = False
        software.m2_camera_enabled = True

    if not token:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Ensure live camera is started (RealSense on device index by default)
    if not token:
        try:
            software.start_live_camera(device_index=device_index)
        except Exception as e:
            print(f"[Warning] Could not open camera on device {device_index}: {e}")

    # Active websocket connections
    active_websockets: List[WebSocket] = []

    # Stream logs to websockets
    def on_log_message(msg: str):
        payload = json.dumps({"type": "log", "message": msg})
        # Schedule broadcast on running event loop if possible
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                for ws in list(active_websockets):
                    asyncio.run_coroutine_threadsafe(ws.send_text(payload), loop)
        except Exception:
            pass

    software.log_listeners.append(on_log_message)

    # State variables
    app_state = {
        "is_looping": False,
        "work_scope": work_scope or "Knowledge worker software development, document reading, research, and work-related communications",
        "current_goal": current_goal or "Implement and verify SYA zero-training offline replay system",
        "window_size": window_size,
        "step_size": step_size,
        "last_record": None,
        "total_windows": 0,
        "camera_error": None,
        "desktop_screen_file": None,
        "desktop_accessibility": None,
        "input_revision": 0,
        "loop_revision": 0,
        "last_error": None,
    }

    active_loop_task: Optional[asyncio.Task] = None
    inference_lock = asyncio.Lock()
    is_inferring: bool = False

    # MJPEG generator
    def mjpeg_generator():
        while True:
            if software.camera_master_active and software.camera:
                frame_bytes = software.camera.get_latest_jpeg()
                if frame_bytes:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                    )
            time.sleep(0.05)

    @app.get("/video_feed")
    def video_feed():
        """MJPEG live stream from connected camera."""
        return StreamingResponse(
            mjpeg_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @app.get("/api/camera_snapshot")
    def camera_snapshot():
        frame = software.camera.get_latest_jpeg() if software.camera_master_active and software.camera else None
        if frame:
            return Response(frame, media_type="image/jpeg")
        return JSONResponse({"status": "camera_unavailable"}, status_code=404)

    @app.get("/screen_feed")
    def screen_feed():
        """Current screen capture."""
        if token:
            filename = app_state["desktop_screen_file"] if software.screen_master_active else None
            if filename and os.path.isfile(filename):
                return FileResponse(filename, media_type="image/png")
            return JSONResponse({"status": "screen_unavailable"}, status_code=404)
        jpeg = software.screen_provider.get_latest_jpeg()
        if jpeg:
            return StreamingResponse(iter([jpeg]), media_type="image/jpeg")
        return JSONResponse({"status": "no_screenshot"}, status_code=404)

    @app.get("/api/status")
    def get_status():
        cam_running = bool(software.camera and software.camera.is_running and software.camera.get_latest_jpeg())
        cam_name = software.camera.camera_name if software.camera else "Disconnected"
        rss = software.process.memory_info().rss / (1024 * 1024)
        if token:
            metadata = app_state["desktop_accessibility"]
            acc_snap = {"app_name": metadata.get("appName"), "window_title": metadata.get("windowTitle")} if metadata else None
        else:
            acc_snap = software.accessibility_provider.last_snapshot.to_dict() if software.accessibility_provider.last_snapshot else None
        return {
            "camera_connected": cam_running,
            "camera_name": cam_name,
            "camera_error": app_state["camera_error"] or (software.camera.stream_error if software.camera else None),
            "last_error": app_state["last_error"],
            "is_looping": app_state["is_looping"],
            "is_inferring": is_inferring,
            "work_scope": app_state["work_scope"],
            "current_goal": app_state["current_goal"],
            "window_size": app_state["window_size"],
            "step_size": app_state["step_size"],
            "process_rss_mb": round(rss, 1),
            "last_record": app_state["last_record"],
            "total_windows": app_state["total_windows"],
            # Master sensor states
            "camera_master_active": software.camera_master_active,
            "screen_master_active": software.screen_master_active,
            "accessibility_master_active": software.accessibility_master_active,
            # Module sub-switch states
            "m1_camera_enabled": software.m1_camera_enabled,
            "m2_camera_enabled": software.m2_camera_enabled,
            "m2_screen_enabled": software.m2_screen_enabled,
            "m2_accessibility_enabled": software.m2_accessibility_enabled,
            "accessibility_preview": acc_snap
        }

    class ConfigRequest(BaseModel):
        work_scope: Optional[str] = None
        current_goal: Optional[str] = None
        window_size: Optional[float] = None
        step_size: Optional[float] = None
        device_index: Optional[int] = None
        camera_master_active: Optional[bool] = None
        screen_master_active: Optional[bool] = None
        accessibility_master_active: Optional[bool] = None
        m1_camera_enabled: Optional[bool] = None
        m2_camera_enabled: Optional[bool] = None
        m2_screen_enabled: Optional[bool] = None
        m2_accessibility_enabled: Optional[bool] = None
        desktop_screen_file: Optional[str] = None
        desktop_accessibility: Optional[Dict[str, Any]] = None

    @app.post("/api/config")
    def update_config(req: ConfigRequest):
        if req.work_scope is not None:
            app_state["work_scope"] = req.work_scope
        if req.current_goal is not None:
            app_state["current_goal"] = req.current_goal
        if req.window_size is not None:
            app_state["window_size"] = req.window_size
        if req.step_size is not None:
            app_state["step_size"] = req.step_size
        fields = req.model_fields_set
        changed = any(
            getattr(req, name) is not None and getattr(req, name) != getattr(software, name)
            for name in ("camera_master_active", "screen_master_active", "accessibility_master_active",
                         "m1_camera_enabled", "m2_camera_enabled", "m2_screen_enabled", "m2_accessibility_enabled")
        )
        revoked_evidence = token and any(
            name in fields and app_state[name] is not None and getattr(req, name) is None
            for name in ("desktop_screen_file", "desktop_accessibility")
        )
        if changed or revoked_evidence:
            app_state["input_revision"] += 1
            app_state["last_record"] = None
        if token and "desktop_screen_file" in fields:
            app_state["desktop_screen_file"] = req.desktop_screen_file
        if token and "desktop_accessibility" in fields:
            app_state["desktop_accessibility"] = req.desktop_accessibility
        if not token and req.device_index is not None:
            software.start_live_camera(device_index=req.device_index)
        if req.camera_master_active is not None:
            was_active = software.camera_master_active
            software.camera_master_active = req.camera_master_active
            if token and req.camera_master_active != was_active:
                app_state["camera_error"] = None
                if req.camera_master_active:
                    try:
                        index = req.device_index if req.device_index is not None else device_index
                        software.start_live_camera(device_index=index)
                    except Exception:
                        software.stop_live_camera()
                        app_state["camera_error"] = "camera_unavailable"
                else:
                    software.stop_live_camera()
        if req.screen_master_active is not None:
            software.screen_master_active = req.screen_master_active
        if req.accessibility_master_active is not None:
            software.accessibility_master_active = req.accessibility_master_active
        if req.m1_camera_enabled is not None:
            software.m1_camera_enabled = req.m1_camera_enabled
        if req.m2_camera_enabled is not None:
            software.m2_camera_enabled = req.m2_camera_enabled
        if req.m2_screen_enabled is not None:
            software.m2_screen_enabled = req.m2_screen_enabled
        if req.m2_accessibility_enabled is not None:
            software.m2_accessibility_enabled = req.m2_accessibility_enabled

        return {
            "status": "ok",
            "work_scope": app_state["work_scope"],
            "current_goal": app_state["current_goal"],
            "window_size": app_state["window_size"],
            "step_size": app_state["step_size"],
            "camera_master_active": software.camera_master_active,
            "screen_master_active": software.screen_master_active,
            "accessibility_master_active": software.accessibility_master_active,
            "m1_camera_enabled": software.m1_camera_enabled,
            "m2_camera_enabled": software.m2_camera_enabled,
            "m2_screen_enabled": software.m2_screen_enabled,
            "m2_accessibility_enabled": software.m2_accessibility_enabled
        }

    class TriggerRequest(BaseModel):
        injected_accessibility: Optional[Dict[str, Any]] = None

    async def _do_inference(is_manual: bool = False, injected_accessibility: Optional[Dict[str, Any]] = None):
        nonlocal is_inferring
        if inference_lock.locked():
            if is_manual:
                return {"status": "busy", "message": "Inference currently in progress, please wait a moment..."}
            return None

        async with inference_lock:
            is_inferring = True
            software.log("[Observer] Starting live window inference...")
            status_payload = json.dumps({"type": "inferring", "is_inferring": True})
            for ws in list(active_websockets):
                try:
                    await ws.send_text(status_payload)
                except Exception:
                    pass

            loop = asyncio.get_running_loop()
            revision = app_state["input_revision"]
            desktop_screen_file = app_state["desktop_screen_file"]
            if token:
                injected_accessibility = app_state["desktop_accessibility"]
            parameters = {
                "window_size_sec": app_state["window_size"],
                "work_scope": app_state["work_scope"],
                "current_goal": app_state["current_goal"],
                **{name: getattr(software, name) for name in (
                    "camera_master_active", "screen_master_active", "accessibility_master_active",
                    "m1_camera_enabled", "m2_camera_enabled", "m2_screen_enabled", "m2_accessibility_enabled"
                )},
            }
            try:
                record = await loop.run_in_executor(
                    None,
                    lambda: software.infer_live_window(
                        **parameters,
                        injected_accessibility=injected_accessibility,
                        desktop_managed=bool(token),
                        desktop_screen_file=desktop_screen_file,
                    )
                )
                if revision != app_state["input_revision"]:
                    return {"status": "superseded"}
                data = record.model_dump()
                app_state["last_record"] = data
                app_state["last_error"] = None
                app_state["total_windows"] += 1
                payload = json.dumps({"type": "window_result", "record": data})
                for ws in list(active_websockets):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        pass
                return {"status": "success", "record": data}
            except Exception as e:
                app_state["last_error"] = "inference_failed"
                err_msg = f"Inference failed: {str(e)}"
                software.log(f"[Error] {err_msg}")
                return {"status": "error", "message": err_msg}
            finally:
                is_inferring = False
                status_payload = json.dumps({"type": "inferring", "is_inferring": False})
                for ws in list(active_websockets):
                    try:
                        await ws.send_text(status_payload)
                    except Exception:
                        pass

    @app.post("/api/trigger_window")
    async def trigger_window(req: Optional[TriggerRequest] = None):
        """Trigger a single live window inference."""
        injected_acc = req.injected_accessibility if req else None
        res = await _do_inference(is_manual=True, injected_accessibility=injected_acc)
        if res and res.get("status") == "error":
            return JSONResponse(res, status_code=500)
        return res or {"status": "busy", "message": "Inference in progress"}

    async def continuous_loop(revision):
        software.log("▶ Continuous auto-tracking loop started.")
        # Allow 2 seconds for camera buffer to gather initial frames
        await asyncio.sleep(2.0)
        while app_state["is_looping"] and revision == app_state["loop_revision"]:
            try:
                await _do_inference(is_manual=False)
            except Exception as e:
                software.log(f"[Loop Error] {e}")

            # Sleep step size
            await asyncio.sleep(app_state["step_size"])

    @app.post("/api/start_loop")
    async def start_loop():
        nonlocal active_loop_task
        if app_state["is_looping"]:
            return {"status": "already_running"}
        app_state["is_looping"] = True
        active_loop_task = asyncio.create_task(continuous_loop(app_state["loop_revision"]))
        return {"status": "started"}

    @app.post("/api/stop_loop")
    async def stop_loop():
        nonlocal active_loop_task
        app_state["is_looping"] = False
        app_state["loop_revision"] += 1
        app_state["input_revision"] += 1
        software.log("⏹ Continuous auto-tracking loop stopped.")
        return {"status": "stopped"}

    @app.on_event("startup")
    async def on_startup():
        nonlocal active_loop_task
        if token:
            return
        software.log("[System] Auto-starting continuous tracking loop...")
        app_state["is_looping"] = True
        active_loop_task = asyncio.create_task(continuous_loop(app_state["loop_revision"]))

    @app.on_event("shutdown")
    async def on_shutdown():
        if active_loop_task:
            active_loop_task.cancel()
        software.stop_live_camera()

    @app.websocket("/ws/logs")
    async def websocket_logs(websocket: WebSocket):
        if token:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        active_websockets.append(websocket)
        # Send initial status
        rss = software.process.memory_info().rss / (1024 * 1024)
        init_payload = json.dumps({
            "type": "init",
            "camera_name": software.camera.camera_name if software.camera else "None",
            "process_rss_mb": round(rss, 1),
            "last_record": app_state["last_record"]
        })
        await websocket.send_text(init_payload)

        try:
            while True:
                # Keep alive
                data = await websocket.receive_text()
        except WebSocketDisconnect:
            if websocket in active_websockets:
                active_websockets.remove(websocket)

    @app.get("/", response_class=HTMLResponse)
    def index_page():
        return HTMLResponse(content=HTML_CONTENT, status_code=200)

    return app


HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>SYA 模块输入输出观察与调试台 (RealSense + MLX Qwen3-VL)</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --accent: #818cf8;
      --success: #34d399;
      --warning: #fbbf24;
      --danger: #f87171;
      --code-bg: #090d16;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 16px;
      line-height: 1.5;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 16px;
    }
    .title-group h1 { font-size: 1.25rem; font-weight: 700; color: var(--primary); }
    .title-group p { font-size: 0.85rem; color: var(--text-muted); }
    .status-badges { display: flex; gap: 8px; align-items: center; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600;
      background: rgba(255,255,255,0.06); border: 1px solid var(--card-border);
    }
    .badge-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); }
    .badge-dot.live { background: var(--success); box-shadow: 0 0 8px var(--success); }

    /* Controls card */
    .control-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 14px 18px;
      margin-bottom: 16px;
      display: grid;
      grid-template-columns: 2fr 2fr 1fr auto;
      gap: 12px;
      align-items: end;
    }
    .input-field label {
      display: block; font-size: 0.75rem; font-weight: 600; color: var(--text-muted); margin-bottom: 4px;
    }
    .input-field input {
      width: 100%; background: var(--code-bg); border: 1px solid var(--card-border);
      color: var(--text); padding: 8px 10px; border-radius: 6px; font-size: 0.85rem;
    }
    .btn-group { display: flex; gap: 8px; }
    button {
      cursor: pointer; border: none; border-radius: 6px; padding: 8px 16px;
      font-weight: 600; font-size: 0.85rem; transition: all 0.15s ease;
    }
    button.btn-primary { background: var(--primary); color: #0284c7; font-weight: 700; }
    button.btn-primary:hover { opacity: 0.9; }
    button.btn-success { background: #059669; color: white; }
    button.btn-danger { background: #dc2626; color: white; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Main Grid */
    .modules-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .card-header {
      padding: 10px 14px;
      background: rgba(255,255,255,0.03);
      border-bottom: 1px solid var(--card-border);
      font-size: 0.85rem;
      font-weight: 700;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .card-body { padding: 12px 14px; flex: 1; display: flex; flex-direction: column; gap: 10px; }

    .media-box {
      width: 100%; height: 180px; background: #000; border-radius: 6px;
      overflow: hidden; display: flex; align-items: center; justify-content: center;
      position: relative; border: 1px solid var(--card-border);
    }
    .media-box img { width: 100%; height: 100%; object-fit: contain; }
    .media-placeholder { font-size: 0.8rem; color: var(--text-muted); }

    .kv-row { display: flex; justify-content: space-between; font-size: 0.8rem; }
    .kv-label { color: var(--text-muted); }
    .kv-val { font-weight: 600; }

    /* Prediction Badges */
    .pred-badge {
      display: inline-block; padding: 4px 10px; border-radius: 6px; font-weight: 700; font-size: 0.85rem;
    }
    .pred-engaged { background: rgba(52, 211, 153, 0.2); color: #34d399; border: 1px solid #34d399; }
    .pred-distracted { background: rgba(251, 191, 36, 0.2); color: #fbbf24; border: 1px solid #fbbf24; }
    .pred-rejected { background: rgba(248, 113, 113, 0.2); color: #f87171; border: 1px solid #f87171; }
    .pred-null { background: rgba(148, 163, 184, 0.2); color: #94a3b8; border: 1px solid #94a3b8; }

    .bar-container { height: 8px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden; display: flex; }
    .bar-fill { height: 100%; transition: width 0.3s; }
    .bar-eng { background: var(--success); }
    .bar-dist { background: var(--warning); }

    .evidence-box {
      background: var(--code-bg); border: 1px solid var(--card-border);
      border-radius: 6px; padding: 8px 10px; font-size: 0.75rem; color: #cbd5e1;
      max-height: 90px; overflow-y: auto; font-family: monospace; line-height: 1.4;
    }

    /* Terminal console */
    .terminal-section {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      overflow: hidden;
    }
    .terminal-header {
      background: rgba(255,255,255,0.04);
      padding: 8px 14px;
      font-size: 0.8rem;
      font-weight: 700;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--card-border);
    }
    .terminal-body {
      padding: 12px 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.78rem;
      color: #38bdf8;
      height: 240px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .term-line { margin-bottom: 3px; }
    .term-err { color: #f87171; }
    .term-sys { color: #a78bfa; }
  </style>
</head>
<body>

  <header>
    <div class="title-group">
      <h1>SYA 模块输入输出观察与调试台</h1>
      <p>无需训练离线闭环：Intel RealSense L515 摄像头 + Apple M4 (MLX Qwen3-VL-4B)</p>
    </div>
    <div class="status-badges">
      <div id="infer-indicator" class="badge" style="display:none;background:rgba(251,191,36,0.15);border-color:var(--warning);color:var(--warning);">
        <div class="badge-dot" style="background:var(--warning);box-shadow:0 0 8px var(--warning);"></div>
        <span id="txt-infer">⏳ 正在计算实时窗口 (M1 + M2)...</span>
      </div>
      <div class="badge"><div id="dot-ws" class="badge-dot"></div> <span id="txt-ws">WebSocket: 连接中</span></div>
      <div class="badge"><div class="badge-dot live"></div> <span id="txt-cam">Camera: 探测中</span></div>
      <div class="badge">M4 统一内存: <span id="txt-rss" style="color:var(--primary);margin-left:4px;">0 MB</span></div>
    </div>
  </header>

  <!-- Controls -->
  <div class="control-card">
    <div class="input-field">
      <label>Declared Work Scope (工作范围)</label>
      <input type="text" id="inp-scope" value="Knowledge worker software development, document reading, research, and work-related communications">
    </div>
    <div class="input-field">
      <label>Current Concrete Goal (当前具体目标)</label>
      <input type="text" id="inp-goal" value="Implement and verify SYA zero-training offline replay system">
    </div>
    <div class="input-field">
      <label>Window Duration (秒)</label>
      <input type="number" id="inp-win" value="6.0" step="1.0" min="4.0" max="20.0">
    </div>
    <div class="btn-group">
      <button class="btn-primary" id="btn-trigger" onclick="triggerSingleWindow()">⚡ 单窗实时分析</button>
      <button class="btn-success" id="btn-loop" onclick="toggleLoop()">▶ 启动连续循环</button>
    </div>
  </div>

  <!-- 3 Module Columns -->
  <div class="modules-grid">

    <!-- M1 Webcam Card -->
    <div class="card">
      <div class="card-header">
        <span>📹 M1: RealSense 摄像头注意估计</span>
        <label style="font-size:0.75rem;font-weight:normal;display:flex;align-items:center;gap:4px;cursor:pointer;">
          <input type="checkbox" id="chk-m1-cam" checked onchange="updateSwitches()">
          读取摄像头
        </label>
        <span id="m1-lat" style="color:var(--text-muted);font-size:0.75rem;">-- ms</span>
      </div>
      <div class="card-body">
        <div class="media-box">
          <img id="cam-feed" src="/video_feed" onerror="this.style.display='none';document.getElementById('cam-alt').style.display='block';">
          <div id="cam-alt" class="media-placeholder" style="display:none;">摄像头画面加载中...</div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span style="font-size:0.8rem;color:var(--text-muted);">Engagement 估计</span>
          <span id="m1-badge" class="pred-badge pred-null">等待输入</span>
        </div>

        <div>
          <div style="display:flex; justify-content:space-between; font-size:0.72rem; color:var(--text-muted); margin-bottom:2px;">
            <span>Engaged: <b id="m1-eng-pct">--%</b></span>
            <span>Distracted: <b id="m1-dist-pct">--%</b></span>
          </div>
          <div class="bar-container">
            <div id="bar-eng" class="bar-fill bar-eng" style="width: 50%;"></div>
            <div id="bar-dist" class="bar-fill bar-dist" style="width: 50%;"></div>
          </div>
        </div>

        <div class="kv-row">
          <span class="kv-label">有效人脸帧数:</span>
          <span class="kv-val" id="m1-faces">0 / 0</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">人脸质量 / 状态:</span>
          <span class="kv-val" id="m1-quality">--</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">拒判原因:</span>
          <span class="kv-val" id="m1-reject" style="color:var(--warning);">无</span>
        </div>
      </div>
    </div>

    <!-- M2 Screen Card -->
    <div class="card">
      <div class="card-header">
        <span>🖥️ M2: 屏幕工作与目标对齐 (MLX Qwen3-VL)</span>
        <span id="m2-lat" style="color:var(--text-muted);font-size:0.75rem;">-- ms</span>
      </div>
      <div class="card-body">
        <div style="display:flex; gap:10px; font-size:0.75rem; background:rgba(255,255,255,0.03); padding:4px 8px; border-radius:4px; border:1px solid var(--card-border);">
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
            <input type="checkbox" id="chk-m2-cam" checked onchange="updateSwitches()"> 摄像头视角
          </label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
            <input type="checkbox" id="chk-m2-scr" checked onchange="updateSwitches()"> 屏幕画面
          </label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
            <input type="checkbox" id="chk-m2-acc" checked onchange="updateSwitches()"> 无障碍语义
          </label>
        </div>

        <div class="media-box">
          <img id="screen-img" src="/screen_feed" alt="Screen Capture">
        </div>

        <div style="display:flex; gap:10px; align-items:center;">
          <div style="flex:1;">
            <span style="font-size:0.72rem;color:var(--text-muted);display:block;">工作相关性 W_hat</span>
            <span id="m2-w" class="pred-badge pred-null">等待输入</span>
          </div>
          <div style="flex:1;">
            <span style="font-size:0.72rem;color:var(--text-muted);display:block;">目标一致性 Goal</span>
            <span id="m2-goal" class="pred-badge pred-null">等待输入</span>
          </div>
        </div>

        <div class="kv-row">
          <span class="kv-label">前台活动应用:</span>
          <span class="kv-val" id="m2-app">--</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">无障碍语义源:</span>
          <span class="kv-val" id="m2-acc-app" style="color:var(--accent);font-size:0.75rem;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">--</span>
        </div>

        <div>
          <span style="font-size:0.72rem;color:var(--text-muted);display:block;margin-bottom:3px;">多模态证据解析 (Qwen3-VL Evidence):</span>
          <div id="m2-ev" class="evidence-box">暂无证据</div>
        </div>
      </div>
    </div>

    <!-- M3 Coordinator & Boundaries Card -->
    <div class="card">
      <div class="card-header">
        <span>⏱️ M3: 因果协调与科学边界隔离</span>
        <span id="m3-lat" style="color:var(--text-muted);font-size:0.75rem;">-- ms</span>
      </div>
      <div class="card-body">
        <div class="kv-row">
          <span class="kv-label">当前观测区间:</span>
          <span class="kv-val" id="m3-win" style="color:var(--primary);">0.0s - 0.0s</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">窗口判定状态:</span>
          <span class="kv-val" id="m3-status">未执行</span>
        </div>

        <hr style="border:none;border-top:1px dashed var(--card-border);margin:4px 0;">

        <div style="font-size:0.75rem;font-weight:700;color:var(--accent);margin-bottom:4px;">严格科学边界执行状态:</div>
        <div class="kv-row">
          <span class="kv-label">P (生理注意支持):</span>
          <span class="kv-val" style="color:#a78bfa;">NULL (未训练/未验证)</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">W×P 状态四分类:</span>
          <span class="kv-val" style="color:#a78bfa;">NULL (严禁冒名C11~C00)</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">work_focus (专注真值):</span>
          <span class="kv-val" style="color:#a78bfa;">NULL (未校准)</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">因果时序隔离:</span>
          <span class="kv-val" style="color:var(--success);">PASSED (无前瞻)</span>
        </div>

        <hr style="border:none;border-top:1px dashed var(--card-border);margin:4px 0;">

        <div class="kv-row">
          <span class="kv-label">总处理耗时:</span>
          <span class="kv-val" id="m3-totallat">-- ms</span>
        </div>
        <div class="kv-row">
          <span class="kv-label">单进程峰值 RSS:</span>
          <span class="kv-val" id="m3-rss">-- MB</span>
        </div>
      </div>
    </div>

  </div>

  <!-- Live Terminal Console -->
  <div class="terminal-section">
    <div class="terminal-header">
      <span>🖥️ SHELL & 模块调试输出日志流 (Live Stream)</span>
      <button style="background:transparent;border:1px solid var(--card-border);color:var(--text-muted);font-size:0.75rem;padding:3px 8px;" onclick="clearLogs()">清空日志</button>
    </div>
    <div id="term" class="terminal-body"></div>
  </div>

  <script>
    let ws = null;
    let isLooping = false;

    function logToTerm(text, cls = '') {
      const term = document.getElementById('term');
      const div = document.createElement('div');
      div.className = 'term-line ' + cls;
      div.textContent = text;
      term.appendChild(div);
      term.scrollTop = term.scrollHeight;
    }

    function clearLogs() {
      document.getElementById('term').innerHTML = '';
    }

    function initWebSocket() {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${window.location.host}/ws/logs`);

      ws.onopen = () => {
        document.getElementById('dot-ws').className = 'badge-dot live';
        document.getElementById('txt-ws').textContent = 'WebSocket: 已连接';
        logToTerm('[WebSocket] 连接成功，开始接收后端实时日志流...', 'term-sys');
      };

      ws.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data);
          if (data.type === 'log') {
            logToTerm(data.message);
          } else if (data.type === 'inferring') {
            const ind = document.getElementById('infer-indicator');
            if (ind) ind.style.display = data.is_inferring ? 'inline-flex' : 'none';
          } else if (data.type === 'window_result') {
            updateDashboard(data.record);
          } else if (data.type === 'init') {
            if (data.camera_name) {
              document.getElementById('txt-cam').textContent = 'Camera: ' + data.camera_name;
            }
            if (data.process_rss_mb) {
              document.getElementById('txt-rss').textContent = data.process_rss_mb + ' MB';
            }
            if (data.last_record) {
              updateDashboard(data.last_record);
            }
          }
        } catch (e) {
          logToTerm(evt.data);
        }
      };

      ws.onclose = () => {
        document.getElementById('dot-ws').className = 'badge-dot';
        document.getElementById('txt-ws').textContent = 'WebSocket: 断开重连中...';
        setTimeout(initWebSocket, 2000);
      };
    }

    function updateDashboard(rec) {
      if (!rec) return;

      // Update Screen preview image
      document.getElementById('screen-img').src = '/screen_feed?t=' + Date.now();

      // M1
      const p = rec.person || {};
      const pred = p.engagement_pred || 'Rejected';
      const m1Badge = document.getElementById('m1-badge');
      m1Badge.textContent = pred;
      m1Badge.className = 'pred-badge ' + (pred === 'Engaged' ? 'pred-engaged' : (pred === 'Distracted' ? 'pred-distracted' : 'pred-rejected'));

      if (p.engagement_scores) {
        const eng = Math.round((p.engagement_scores.Engaged || 0) * 100);
        const dist = Math.round((p.engagement_scores.Distracted || 0) * 100);
        document.getElementById('m1-eng-pct').textContent = eng + '%';
        document.getElementById('m1-dist-pct').textContent = dist + '%';
        document.getElementById('bar-eng').style.width = eng + '%';
        document.getElementById('bar-dist').style.width = dist + '%';
      }

      document.getElementById('m1-faces').textContent = `${p.valid_face_frames || 0} / ${p.total_sampled_frames || 0}`;
      document.getElementById('m1-quality').textContent = p.quality_status || '--';
      document.getElementById('m1-reject').textContent = p.rejection_reason || '无';
      document.getElementById('m1-lat').textContent = `${p.latency_ms || 0} ms`;

      // M2
      const s = rec.screen || {};
      const m2W = document.getElementById('m2-w');
      const m2Goal = document.getElementById('m2-goal');

      m2W.textContent = String(s.W_hat);
      m2W.className = 'pred-badge ' + (s.W_hat === true ? 'pred-engaged' : (s.W_hat === false ? 'pred-distracted' : 'pred-null'));

      m2Goal.textContent = String(s.goal_aligned);
      m2Goal.className = 'pred-badge ' + (s.goal_aligned === true ? 'pred-engaged' : (s.goal_aligned === false ? 'pred-distracted' : 'pred-null'));

      document.getElementById('m2-app').textContent = s.active_app || 'Unknown';
      document.getElementById('m2-ev').textContent = s.brief_evidence || '--';
      document.getElementById('m2-lat').textContent = `${s.latency_ms || 0} ms`;

      // M3
      const w = rec.window || {};
      document.getElementById('m3-win').textContent = `${w.start}s - ${w.end}s`;
      document.getElementById('m3-status').textContent = rec.status || '--';
      document.getElementById('m3-totallat').textContent = `${w.latency_ms || 0} ms`;

      const m = rec.metrics || {};
      if (m.process_rss_mb) {
        document.getElementById('m3-rss').textContent = `${m.process_rss_mb} MB`;
        document.getElementById('txt-rss').textContent = `${m.process_rss_mb} MB`;
      }
    }

    async function triggerSingleWindow() {
      const btn = document.getElementById('btn-trigger');
      btn.disabled = true;
      btn.textContent = '⏳ 推理中...';
      logToTerm('[UI] 触发单窗推理 (RealSense + Screen)...', 'term-sys');

      // Update config first
      await fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          work_scope: document.getElementById('inp-scope').value,
          current_goal: document.getElementById('inp-goal').value,
          window_size: parseFloat(document.getElementById('inp-win').value)
        })
      });

      try {
        const res = await fetch('/api/trigger_window', { method: 'POST' });
        const json = await res.json();
        if (json.status === 'success') {
          updateDashboard(json.record);
        } else {
          logToTerm('[Error] ' + json.message, 'term-err');
        }
      } catch (err) {
        logToTerm('[Error] 网络请求失败: ' + err, 'term-err');
      } finally {
        btn.disabled = false;
        btn.textContent = '⚡ 单窗实时分析';
      }
    }

    async function updateSwitches() {
      const m1Cam = document.getElementById('chk-m1-cam').checked;
      const m2Cam = document.getElementById('chk-m2-cam').checked;
      const m2Scr = document.getElementById('chk-m2-scr').checked;
      const m2Acc = document.getElementById('chk-m2-acc').checked;
      await fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          m1_camera_enabled: m1Cam,
          m2_camera_enabled: m2Cam,
          m2_screen_enabled: m2Scr,
          m2_accessibility_enabled: m2Acc
        })
      });
      logToTerm(`[Config] 切换传感器读取: M1Cam=${m1Cam}, M2Cam=${m2Cam}, M2Scr=${m2Scr}, M2Acc=${m2Acc}`, 'term-sys');
    }

    async function toggleLoop() {
      const btn = document.getElementById('btn-loop');
      if (!isLooping) {
        // Start
        btn.disabled = true;
        await fetch('/api/start_loop', { method: 'POST' });
        isLooping = true;
        btn.textContent = '⏹ 停止连续跟踪';
        btn.className = 'btn-danger';
        btn.disabled = false;
      } else {
        // Stop
        btn.disabled = true;
        await fetch('/api/stop_loop', { method: 'POST' });
        isLooping = false;
        btn.textContent = '▶ 启动连续循环';
        btn.className = 'btn-success';
        btn.disabled = false;
      }
    }

    // Initialize
    window.addEventListener('DOMContentLoaded', () => {
      initWebSocket();
      fetch('/api/status').then(r => r.json()).then(data => {
        if (data.camera_name) document.getElementById('txt-cam').textContent = 'Camera: ' + data.camera_name;
        if (data.process_rss_mb) document.getElementById('txt-rss').textContent = data.process_rss_mb + ' MB';
        if (data.m1_camera_enabled !== undefined) document.getElementById('chk-m1-cam').checked = data.m1_camera_enabled;
        if (data.m2_camera_enabled !== undefined) document.getElementById('chk-m2-cam').checked = data.m2_camera_enabled;
        if (data.m2_screen_enabled !== undefined) document.getElementById('chk-m2-scr').checked = data.m2_screen_enabled;
        if (data.m2_accessibility_enabled !== undefined) document.getElementById('chk-m2-acc').checked = data.m2_accessibility_enabled;
        if (data.accessibility_preview) {
          const snap = data.accessibility_preview;
          document.getElementById('m2-acc-app').textContent = (snap.app_name || '--') + (snap.window_title ? ` - ${snap.window_title}` : '');
        }
        if (data.last_record) updateDashboard(data.last_record);
        if (data.is_looping) {
          isLooping = true;
          const btn = document.getElementById('btn-loop');
          btn.textContent = '⏹ 停止连续跟踪';
          btn.className = 'btn-danger';
        }
        if (data.is_inferring) {
          const ind = document.getElementById('infer-indicator');
          if (ind) ind.style.display = 'inline-flex';
        }
      });

      // Periodic 3-second sync to ensure UI always stays up to date
      setInterval(async () => {
        try {
          const res = await fetch('/api/status');
          const data = await res.json();
          if (data.last_record && (!window.lastWindowId || data.last_record.window_id !== window.lastWindowId)) {
            window.lastWindowId = data.last_record.window_id;
            updateDashboard(data.last_record);
          }
          if (data.process_rss_mb) document.getElementById('txt-rss').textContent = data.process_rss_mb + ' MB';
          if (data.is_inferring !== undefined) {
            const ind = document.getElementById('infer-indicator');
            if (ind) ind.style.display = data.is_inferring ? 'inline-flex' : 'none';
          }
        } catch (e) {}
      }, 3000);
    });
  </script>
</body>
</html>
"""


def run_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    software: Optional[SYASoftware] = None,
    work_scope: Optional[str] = None,
    current_goal: Optional[str] = None,
    window_size: float = 6.0,
    step_size: float = 10.0,
    device_index: int = 0
):
    """Run the FastAPI web server with Uvicorn."""
    app = create_app(
        sya_app=software,
        work_scope=work_scope,
        current_goal=current_goal,
        window_size=window_size,
        step_size=step_size,
        device_index=device_index
    )
    print("\n" + "=" * 65)
    print("      SYA DEBUGGER & OBSERVER WEB DASHBOARD")
    print(f"      Access URL: http://localhost:{port}")
    print(f"      RealSense Camera Stream: http://localhost:{port}/video_feed")
    print("=" * 65 + "\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_server()
