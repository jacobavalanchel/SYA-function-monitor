/**
 * Collect only the frozen runtime's fixed startup protocol from stderr.
 *
 * Keep every dependency inside this function: the Electron harness embeds its
 * toString() in a generated bootstrap. nowMs must return host monotonic elapsed
 * milliseconds measured from immediately before spawn. A receipt is measured
 * when the chunk containing the line terminator arrives; it is not an emission
 * timestamp. Python durations use only Python timestamps, never the host clock.
 *
 * Retained state is bounded by 128 ASCII candidate characters, 13 stage records,
 * and a fixed set of reason codes. Unrelated stderr is discarded, not reported.
 */
export function createStartupTraceCollector({ nowMs } = {}) {
  if (typeof nowMs !== 'function') throw new TypeError('Startup trace requires a monotonic nowMs function.');

  const stageNames = [
    'bootstrap', 'entry', 'entry_imports_complete', 'freeze_support_complete',
    'tls_start', 'tls_complete', 'server_import_start', 'server_import_complete',
    'config_start', 'config_complete', 'bind_start', 'bind_complete', 'loop_ready',
  ];
  const familyPrefix = '[sya-startup';
  const maxLineBytes = 128;
  const stages = [];
  const reasonCodes = new Set();
  let prefixLength = 0, candidate = '', discarding = false, finished = false;
  let previousHostMs = 0, firstPythonNs = null, previousPythonNs = null;

  function acceptLine(receiptMs) {
    const line = candidate.endsWith('\r') ? candidate.slice(0, -1) : candidate;
    const version = /^\[sya-startup-v([0-9]+)\] /.exec(line);
    if (version && version[1] !== '1') { reasonCodes.add('unsupported_version'); return; }
    const match = /^\[sya-startup-v1\] ([a-z_]+) ([^ ]+)$/.exec(line);
    if (!match) { reasonCodes.add('invalid_format'); return; }
    const [, stage, timestamp] = match;
    const index = stageNames.indexOf(stage);
    if (index < 0) { reasonCodes.add('unknown_stage'); return; }
    // Bounded canonical decimal, matching the Python emitter's [0, 10**20).
    if (!/^(0|[1-9][0-9]{0,19})$/.test(timestamp)) { reasonCodes.add('invalid_timestamp'); return; }
    if (index < stages.length) { reasonCodes.add('duplicate_stage'); return; }
    if (index !== stages.length) { reasonCodes.add('out_of_order_stage'); return; }
    const pythonNs = BigInt(timestamp);
    if (previousPythonNs !== null && pythonNs < previousPythonNs) { reasonCodes.add('python_clock_regression'); return; }
    if (receiptMs === null) return;
    if (firstPythonNs === null) firstPythonNs = pythonNs;
    stages.push({
      stage,
      pythonMonotonicNs: timestamp,
      pythonElapsedMs: Number(pythonNs - firstPythonNs) / 1e6,
      pythonDeltaMs: previousPythonNs === null ? 0 : Number(pythonNs - previousPythonNs) / 1e6,
      receiptMs,
    });
    previousPythonNs = pythonNs;
  }

  function resetLine() { prefixLength = 0; candidate = ''; discarding = false; }

  function push(chunk) {
    if (finished) return;
    const isString = typeof chunk === 'string';
    // ArrayBuffer.isView works for Buffer/Uint8Array across bootstrap realms.
    if (!isString && !(ArrayBuffer.isView(chunk) && chunk.BYTES_PER_ELEMENT === 1)) {
      reasonCodes.add('invalid_chunk'); return;
    }
    if (!chunk.length) return;
    let receiptMs = null;
    try {
      const value = nowMs();
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) reasonCodes.add('invalid_host_clock');
      else if (value < previousHostMs) reasonCodes.add('host_clock_regression');
      else { receiptMs = value; previousHostMs = value; }
    } catch { reasonCodes.add('invalid_host_clock'); }

    for (let offset = 0; offset < chunk.length; offset += 1) {
      if (discarding) {
        // Skip arbitrarily large noise without decoding, slicing, or retaining it.
        const newline = chunk.indexOf(isString ? '\n' : 10, offset);
        if (newline < 0) return;
        resetLine(); offset = newline; continue;
      }
      const code = isString ? chunk.charCodeAt(offset) : chunk[offset];
      if (code === 10) {
        if (candidate) acceptLine(receiptMs);
        resetLine(); continue;
      }
      if (!candidate) {
        if (code !== familyPrefix.charCodeAt(prefixLength)) { prefixLength = 0; discarding = true; continue; }
        prefixLength += 1;
        if (prefixLength === familyPrefix.length) candidate = familyPrefix;
        continue;
      }
      if (candidate.length === maxLineBytes) {
        reasonCodes.add('marker_too_long'); candidate = ''; discarding = true;
      } else if (code > 127 || code < 0) {
        reasonCodes.add('invalid_format'); candidate = ''; discarding = true;
      } else candidate += String.fromCharCode(code);
    }
  }

  function snapshot({ final = false } = {}) {
    if (final && !finished) {
      if (candidate || prefixLength > 0) reasonCodes.add('truncated_marker');
      resetLine(); finished = true;
    }
    return {
      version: 1,
      status: reasonCodes.size ? 'invalid' : stages.length === stageNames.length ? 'complete' : stages.length ? 'partial' : 'missing',
      finished,
      stages: stages.map(stage => ({ ...stage })),
      missingStages: stageNames.slice(stages.length),
      reasonCodes: [...reasonCodes],
    };
  }

  return { push, snapshot, finish: () => snapshot({ final: true }) };
}
