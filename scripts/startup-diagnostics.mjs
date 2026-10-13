/** Privacy boundary: self-contained for the test-only Electron bootstrap.
 * Host receipt times include pipe buffering and scheduling; import cumulative
 * times are nested and MUST NOT be summed. No arbitrary text is retained.
 */
export function createStartupDiagnosticsCollector({ nowMs, mode = 'off' } = {}) {
  if (!['off', 'bootloader-importtime'].includes(mode) || typeof nowMs !== 'function') throw new TypeError('Invalid startup diagnostic options.');
  const events = [], reasons = new Set();
  const modules = new Set(['inspect', 'pkgutil', 'multiprocessing', 'multiprocessing.spawn', 'matplotlib', 'ssl', '_ssl', 'hashlib', '_hashlib', 'certifi', 'native_tls', 'server', 'realsense_runtime', 'numpy', 'mediapipe', 'PIL', 'pyrealsense2', 'pyrsutils', 'socket', 'http.server', 'startup_trace']);
  const hooks = new Set(['startup_hook', 'pyi_rth_inspect', 'pyi_rth_pkgutil', 'pyi_rth_mplconfig', 'pyi_rth_multiprocessing', 'native_entry']);
  const fixed = new Map([
    ['PyInstaller Bootloader 6.x', 'bootloader_start'],
    ['LOADER: this is parent process of onefile application.', 'onefile_parent'],
    ['LOADER: this is child process of onefile application (main application process).', 'onefile_child'],
    ['LOADER: this is child process of onefile application (spawned subprocess).', 'onefile_subprocess'],
    ['LOADER: extracting files to temporary directory...', 'extract_start'],
    ['LOADER: starting the child process...', 'extract_complete_child_start'],
    ['LOADER: pre-initializing embedded python interpreter...', 'interpreter_preinit'],
    ['LOADER: starting embedded python interpreter...', 'interpreter_start'],
    ['LOADER: setting sys._MEIPASS', 'interpreter_initialized'],
    ['LOADER: importing modules from PKG/CArchive', 'bootstrap_imports'],
    ['LOADER: manually flushing stdout and stderr...', 'interpreter_flush'],
    ['LOADER: cleaning up Python interpreter...', 'interpreter_cleanup'],
    ['LOADER: performing cleanup...', 'parent_cleanup'],
    ['LOADER: end of process reached!', 'process_end'],
  ]);
  let line = '', dropping = false, bytes = 0, discardedLines = 0, droppedBytes = 0, finished = false, lastClock = 0;
  let interpreterPid = null, ambiguousPid = false;
  function observeInterpreter(pid) {
    if (ambiguousPid) return;
    if (interpreterPid !== null && interpreterPid !== pid) {
      ambiguousPid = true; interpreterPid = null;
      reasons.add('ambiguous_import_pid');
      for (const event of events) if (event.kind === 'import') { event.pid = null; event.pidAttribution = 'ambiguous'; }
    } else interpreterPid = pid;
  }
  function record(receiptMs) {
    const value = line.endsWith('\r') ? line.slice(0, -1) : line;
    let event;
    const boot = /^\[PYI-([1-9][0-9]{0,9}):DEBUG\] (.*)$/.exec(value);
    if (boot) {
      const pid = Number(boot[1]), body = boot[2];
      if (fixed.has(body)) event = { kind: 'bootloader', event: fixed.get(body), pid };
      const level = /^LOADER: process level = (-2|-1|0|1|2)$/.exec(body);
      if (level) { event = { kind: 'bootloader', event: 'process_level', pid, level: Number(level[1]) }; if (['1', '2'].includes(level[1])) observeInterpreter(pid); }
      if (['interpreter_preinit', 'interpreter_start', 'onefile_child', 'onefile_subprocess'].includes(event?.event)) observeInterpreter(pid);
      const hook = /^LOADER: running ([a-z_]+)\.py$/.exec(body);
      if (hook && hooks.has(hook[1])) event = { kind: 'hook', hook: hook[1], pid };
      const exit = /^LOADER: child process exited \(return code: (-?[0-9]{1,10})\)$/.exec(body);
      if (exit) event = { kind: 'bootloader', event: 'child_exit', pid, exitCode: Number(exit[1]) };
    } else {
      const imported = /^import time:\s+([0-9]{1,12})\s+\|\s+([0-9]{1,12})\s+\|\s+([A-Za-z0-9_.]+)$/.exec(value);
      if (imported && modules.has(imported[3])) event = { kind: 'import', module: imported[3], selfUs: Number(imported[1]), cumulativeUs: Number(imported[2]), pid: interpreterPid, pidAttribution: ambiguousPid ? 'ambiguous' : interpreterPid === null ? 'unknown' : 'inferred_single_interpreter' };
    }
    if (!event) { discardedLines++; return; }
    if (events.length >= 8192) { reasons.add('record_limit'); discardedLines++; return; }
    events.push({ ...event, receiptMs });
  }
  function push(chunk) {
    if (finished || mode === 'off') return;
    const string = typeof chunk === 'string';
    if (!string && !(ArrayBuffer.isView(chunk) && chunk.BYTES_PER_ELEMENT === 1)) { reasons.add('invalid_chunk'); return; }
    let receiptMs;
    try { receiptMs = nowMs(); } catch { receiptMs = NaN; }
    if (!Number.isFinite(receiptMs) || receiptMs < lastClock || receiptMs < 0) { reasons.add('invalid_clock'); receiptMs = lastClock; }
    lastClock = receiptMs;
    for (let i = 0; i < chunk.length; i++) {
      const code = string ? chunk.charCodeAt(i) : chunk[i];
      bytes++;
      if (bytes > 4 * 1024 * 1024) { droppedBytes++; reasons.add('byte_limit'); line = ''; continue; }
      if (code === 10) { if (!dropping) record(receiptMs); else discardedLines++; line = ''; dropping = false; continue; }
      if (dropping) continue;
      if (line.length >= 4096) { reasons.add('line_limit'); line = ''; dropping = true; }
      else if (code > 127) { line = ''; dropping = true; }
      else line += String.fromCharCode(code);
    }
  }
  function snapshot({ final = false } = {}) {
    if (final && !finished) { if (line || dropping) reasons.add('truncated_line'); line = ''; finished = true; }
    return { version: 1, mode, clock: 'host-monotonic-receipt-ms', cumulativeTimesAdditive: false,
      status: mode === 'off' ? 'off' : reasons.size ? 'incomplete' : events.length ? 'collected' : 'missing',
      finished, events: events.map(event => ({ ...event })), discardedLines, droppedBytes,
      receivedBytes: bytes, reasonCodes: [...reasons] };
  }
  return { push, snapshot, finish: () => snapshot({ final: true }) };
}

/** Shared acceptance check. Import lines contain no PID; single-interpreter
 * attribution is explicitly inferred and is unusable after interleaving.
 */
export function startupDiagnosticsCoverage(result, { mode = 'off', expectedParentPid = null } = {}) {
  if (mode === 'off') return { ok: true, reasonCodes: [], status: 'off' };
  const reasons = [];
  if (mode !== 'bootloader-importtime' || result?.mode !== mode) reasons.push('mode_mismatch');
  if (result?.finished !== true || result?.status !== 'collected' || result?.reasonCodes?.length || result?.droppedBytes) reasons.push('unfinished_or_truncated');
  const events = Array.isArray(result?.events) ? result.events : [];
  const parents = [...new Set(events.filter(e => e.event === 'process_level' && e.level === 0).map(e => e.pid))];
  const children = [...new Set(events.filter(e => e.event === 'process_level' && e.level === 1).map(e => e.pid))];
  const parent = parents[0], child = children[0];
  if (parents.length !== 1 || children.length !== 1 || parent === child ||
      (expectedParentPid !== null && parent !== expectedParentPid) || events.some(e => e.event === 'process_level' && e.level === 2)) reasons.push('process_association');
  const index = (event, pid) => events.findIndex(e => e.event === event && e.pid === pid);
  const extraction = index('extract_start', parent), launch = index('extract_complete_child_start', parent);
  const preinit = index('interpreter_preinit', child), start = index('interpreter_start', child), initialized = index('interpreter_initialized', child);
  if (index('onefile_parent', parent) < 0 || index('onefile_child', child) < 0 || extraction < 0 || launch <= extraction || preinit <= launch || start <= preinit || initialized <= start) reasons.push('startup_boundaries');
  const hooks = ['startup_hook', 'pyi_rth_inspect', 'pyi_rth_pkgutil', 'pyi_rth_mplconfig', 'pyi_rth_multiprocessing', 'native_entry'];
  let previous = initialized;
  for (const hook of hooks) {
    const current = events.findIndex(e => e.kind === 'hook' && e.hook === hook && e.pid === child);
    if (current <= previous) reasons.push('missing_or_unordered_hook');
    previous = current;
  }
  if (!events.some(e => e.kind === 'import' && e.pid === child && e.pidAttribution === 'inferred_single_interpreter') ||
      events.some(e => e.kind === 'import' && e.pidAttribution !== 'inferred_single_interpreter')) reasons.push('importtime_attribution');
  return { ok: reasons.length === 0, status: reasons.length ? 'failed' : 'passed', reasonCodes: [...new Set(reasons)],
    parentPid: Number.isSafeInteger(parent) ? parent : null, childPid: Number.isSafeInteger(child) ? child : null,
    association: 'single_parent_child_bootloader_sequence', importPidAttribution: reasons.includes('importtime_attribution') ? 'unavailable' : 'inferred_single_interpreter' };
}
