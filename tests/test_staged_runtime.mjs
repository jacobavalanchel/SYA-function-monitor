import assert from 'node:assert/strict';
import childProcess, { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { testStagedRuntime } from '../scripts/test-staged-runtime.mjs';
import { runtimeTreeSha256 } from '../scripts/hash-runtime.mjs';
import './test_startup_trace.mjs';

const cli = fileURLToPath(new URL('../scripts/test-staged-runtime.mjs', import.meta.url));
const settings = { startupTimeoutMs: 1200, requestTimeoutMs: 250, stopTimeoutMs: 150 };
const paths = ['/health', '/manifest', '/api/monitor/actions', '/api/monitor/screen/status', '/api/monitor/realsense/frame/latest'];
const startupStages = [
  'bootstrap', 'entry', 'entry_imports_complete', 'freeze_support_complete',
  'tls_start', 'tls_complete', 'server_import_start', 'server_import_complete',
  'config_start', 'config_complete', 'bind_start', 'bind_complete', 'loop_ready',
];
const runtime = String.raw`
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { createHmac } = require('node:crypto');
const { spawn } = require('node:child_process');
const [mode, witness, version] = process.argv.slice(2);
const details = { pid: process.pid, home: process.env.HOME, records: process.env.MONITOR_RECORDS_DIR,
  config: process.env.MONITOR_CONFIG_PATH, envKeys: Object.keys(process.env).sort(),
  configContents: fs.readFileSync(process.env.MONITOR_CONFIG_PATH, 'utf8'),
  bytecode: process.env.PYTHONDONTWRITEBYTECODE, tokenLength: process.env.FUNCTION_RUNTIME_TOKEN?.length,
  proofLength: process.env.FUNCTION_RUNTIME_PROOF_KEY?.length,
  independentSecrets: process.env.FUNCTION_RUNTIME_TOKEN !== process.env.FUNCTION_RUNTIME_PROOF_KEY };
fs.writeFileSync(path.join(witness, 'process.json'), JSON.stringify(details));
const record = value => fs.appendFileSync(path.join(witness, 'calls.jsonl'), JSON.stringify(value) + '\n');
const json = (res, status, value) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); };
if (mode.startsWith('trace-')) {
  const stages = ${JSON.stringify(startupStages)};
  const count = ['trace-exit', 'trace-hang', 'trace-signal'].includes(mode) ? 2 : stages.length;
  for (let index = 0; index < count; index++) {
    fs.writeSync(2, '[sya-startup-v1] ' + stages[index] + ' ' + (9007199254740993000n + BigInt(index) * 250000n) + '\n');
  }
  if (mode === 'trace-malformed') {
    fs.writeSync(2, 'PRIVATE-STARTUP-LOG /private/runtime/config\n');
    fs.writeSync(2, '[sya-startup-v1] private_stage 1\n');
    fs.writeSync(2, '[sya-startup-v1] entry ' + 'PRIVATE-STARTUP-LOG'.repeat(200) + '\n');
  }
  fs.writeFileSync(path.join(witness, 'trace-emitted'), 'emitted');
}
if (mode === 'bad-startup' || mode === 'trace-exit') { console.error('DO-NOT-PRINT-RUNTIME-LOG'); process.exit(7); }
if (mode === 'trace-signal') process.kill(process.pid, 'SIGKILL');
if (mode === 'resist') {
  const worker = spawn(process.execPath, ['-e', 'process.on("SIGTERM",()=>{});setInterval(()=>{},1000)'], { stdio: 'ignore' });
  fs.writeFileSync(path.join(witness, 'worker-pid'), String(worker.pid));
  process.on('SIGTERM', () => {});
} else {
  process.on('SIGTERM', () => {
    if (mode === 'mutate-stop') fs.writeFileSync(path.join(process.cwd(), 'modified-on-stop'), 'changed');
    setTimeout(() => process.exit(0), 20);
  });
}
if (mode === 'hang-startup' || mode === 'trace-hang') { setInterval(() => {}, 1000); }
else http.createServer((req, res) => {
  const authorized = req.headers.authorization === 'Bearer ' + process.env.FUNCTION_RUNTIME_TOKEN;
  record({ method: req.method, url: req.url, authorized });
  if (!authorized) {
    if (mode === 'auth-bypass' && req.method === 'POST') return json(res, 200, { ok: true, data: null });
    if (mode === 'sse-bypass' && req.url === '/api/monitor/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: ready\ndata: {}\n\n'); return;
    }
    return json(res, 401, { ok: false, error: { code: 'UNAUTHORIZED' } });
  }
  if (req.url === '/health') {
    if (mode === 'mutate') fs.writeFileSync(path.join(process.cwd(), 'modified-by-runtime'), 'changed');
    if (mode !== 'missing-proof') res.setHeader('X-SYA-Runtime-Proof',
      createHmac('sha256', ['wrong-proof', 'trace-wrong-proof'].includes(mode) ? 'wrong-key' : process.env.FUNCTION_RUNTIME_PROOF_KEY).update('sya-runtime-v1').digest('hex'));
    if (mode === 'trace-delayed-body') {
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.flushHeaders();
      setTimeout(() => res.end(JSON.stringify({ ok: true, data: { status: 'ok', functionId: 'monitor', version } })), 80);
      return;
    }
    return json(res, 200, { ok: true, data: { status: 'ok', functionId: 'monitor', version: mode === 'wrong-identity' ? 'wrong' : version } });
  }
  if (req.url === '/manifest') return json(res, 200, { ok: true, data: { id: 'monitor', version, runtime: 'local-http' } });
  if (req.url === '/api/monitor/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write('event: ready\ndata: {}\n\n');
    res.on('close', () => fs.writeFileSync(path.join(witness, 'stream-closed'), 'closed')); return;
  }
  if (!['/api/monitor/actions', '/api/monitor/screen/status', '/api/monitor/realsense/frame/latest'].includes(req.url)) {
    fs.writeFileSync(path.join(witness, 'unsafe-success-probe'), req.url); return json(res, 500, { ok: false });
  }
  if (mode === 'bad-envelope') return json(res, 200, { data: null });
  let data = null;
  if (req.url === '/api/monitor/actions') {
    const openapi = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'api.openapi.json'), 'utf8'));
    data = Object.entries(openapi.paths).filter(([route]) => route !== '/api/monitor/events')
      .flatMap(([route, item]) => Object.entries(item).map(([method, operation]) => ({ action: operation.operationId, method: method.toUpperCase(), path: route })));
    if (mode === 'bad-actions-object') data = {};
    if (mode === 'bad-actions-empty') data = [];
    if (mode === 'bad-actions-missing-safe') data = data.filter(entry => entry.path !== '/api/monitor/realsense/frame/latest');
    if (mode === 'bad-actions-id') data[0].action = 'unrelated';
    if (mode === 'bad-actions-method') data[0].method = 'POST';
    if (mode === 'bad-actions-path') data[0].path = '/undeclared';
    if (mode === 'bad-actions-duplicate') data.push(data[0]);
  }
  if (req.url === '/api/monitor/screen/status') {
    data = { isMonitoring: false, captureRateMs: 10000, activeWindow: '', idleTimeSeconds: 0, totalCaptures: 0, lastCaptureTime: null, storagePath: '' };
    if (mode === 'bad-screen-empty') data = {};
    if (mode === 'bad-screen-boolean') data.isMonitoring = 'false';
    if (mode === 'bad-screen-rate-type') data.captureRateMs = '10000';
    if (mode === 'bad-screen-rate-zero') data.captureRateMs = 0;
    if (mode === 'bad-screen-rate-negative') data.captureRateMs = -1;
    if (mode === 'bad-screen-rate-infinite') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true,"data":{"isMonitoring":false,"captureRateMs":1e999}}'); return;
    }
  }
  if (req.url.endsWith('/latest')) {
    if (mode === 'frame-present' || mode === 'bad-frame-dimensions') data = {
      source: 'realsense', frameId: 'synthetic-frame', timestamp: 1, capturedAt: '1970-01-01T00:00:00.001Z',
      frameNumber: 1, width: mode === 'bad-frame-dimensions' ? 0 : 1, height: 1, mimeType: 'image/jpeg', jpegBase64: '/9j/2Q==',
      depth: { available: false, width: 0, height: 0, format: '', depthScaleMeters: null }, attention: null,
    };
    if (mode === 'bad-frame-empty') data = {};
  }
  return json(res, 200, { ok: true, data });
}).listen(Number(process.env.PORT), '127.0.0.1');
`;

function exists(pid) {
  try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}

function fixture(t, mode = 'success') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-stage-test-'));
  const directory = path.join(root, 'stage'), witness = path.join(root, 'witness');
  fs.mkdirSync(path.join(directory, 'bin'), { recursive: true }); fs.mkdirSync(witness);
  const manifest = { id: 'monitor', version: '0.2.0', runtime: 'local-http',
    entry: { command: 'bin/monitor runtime', args: [mode, witness, '0.2.0'] },
    api: { health: { method: 'GET', path: '/health' }, events: { transport: 'sse', path: '/api/monitor/events' } } };
  const operation = (operationId, type = 'application/json') => ({ operationId, responses: { 200: { content: { [type]: {} } } } });
  const safeActions = ['health', 'manifest', 'actions.list', 'screen.status', 'realsense.frame.latest'];
  const openapi = { openapi: '3.1.0', info: { version: '0.2.0' }, paths: Object.fromEntries(paths.map((route, index) => [route, { get: operation(safeActions[index]) }])) };
  openapi.paths['/api/monitor/events'] = { get: operation('events', 'text/event-stream') };
  // Non-safe GET routes must receive only unauthenticated denial probes.
  for (const route of ['/api/monitor/camera/status', '/api/monitor/capabilities', '/api/monitor/realsense/devices', '/api/monitor/settings/status']) openapi.paths[route] = { get: operation(route) };
  openapi.paths['/api/monitor/realsense/start'] = { post: operation('start') };
  openapi.paths['/api/monitor/synthetic/{id}'] = Object.fromEntries(['put', 'patch', 'delete', 'head', 'options'].map(method => [method, { ...operation(method), parameters: [{ name: 'key', in: 'query', required: true }] }]));
  fs.writeFileSync(path.join(directory, 'manifest.json'), JSON.stringify(manifest));
  fs.writeFileSync(path.join(directory, 'api.openapi.json'), JSON.stringify(openapi));
  fs.writeFileSync(path.join(directory, 'bin/monitor runtime'), `#!${process.execPath}\n${runtime}`, { mode: 0o700 });
  t.after(() => {
    const file = path.join(witness, 'process.json');
    if (fs.existsSync(file)) {
      const { pid } = JSON.parse(fs.readFileSync(file));
      // Test-owned fallback only, so a failing regression leaves no fixture alive.
      try { process.kill(-pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    }
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { directory, witness };
}

function checkCleanup(witness) {
  const details = JSON.parse(fs.readFileSync(path.join(witness, 'process.json')));
  assert.equal(exists(details.pid), false, 'actual runtime process must exit');
  assert.equal(fs.existsSync(path.dirname(details.home)), false, 'isolated state must be removed');
  return details;
}

function checkTraceLifecycle(trace, ready = false) {
  assert.equal(trace.finished, true);
  assert.equal(trace.hostTimings.clock, 'node-hrtime-from-spawn-before');
  assert.equal(trace.hostTimings.startupTimeoutMs, settings.startupTimeoutMs);
  assert.equal(trace.hostTimings.spawnBeforeMs, 0);
  assert.equal(trace.lifecycle.spawn, 'spawned');
  assert.equal(trace.lifecycle.spawnErrorCode, null);
  assert.equal(trace.lifecycle.close, 'closed');
  const keys = ['spawnBeforeMs', 'spawnReturnMs', 'spawnEventMs',
    ...(ready ? ['firstHealthHeadersMs', 'firstSuccessfulHealthHeadersMs',
      'firstHealthBodyCompleteMs', 'firstSuccessfulHealthBodyCompleteMs', 'authenticatedReadyMs'] : []),
    'exitMs', 'closeMs'];
  for (const [index, key] of keys.entries()) {
    assert.ok(Number.isFinite(trace.hostTimings[key]), `finite ${key}`);
    assert.ok(trace.hostTimings[key] >= 0, `nonnegative ${key}`);
    if (index > 0) assert.ok(trace.hostTimings[key] >= trace.hostTimings[keys[index - 1]], `ordered ${key}`);
  }
  for (const stage of trace.stages) {
    assert.ok(stage.receiptMs >= trace.hostTimings.spawnBeforeMs);
    assert.ok(stage.receiptMs <= trace.hostTimings.closeMs);
  }
}

function mockSpawn(t, replacement) {
  const original = childProcess.spawn;
  childProcess.spawn = (...args) => replacement(original, ...args);
  syncBuiltinESMExports();
  t.after(() => { childProcess.spawn = original; syncBuiltinESMExports(); });
}

test('optional startup diagnostics collect a complete trace without weakening authenticated smoke', async t => {
  const { directory, witness } = fixture(t, 'trace-complete');
  const before = runtimeTreeSha256(directory);
  const result = await testStagedRuntime(directory, { ...settings, collectStartupTrace: true });
  assert.equal(result.runtimeSha256, before);
  assert.equal(runtimeTreeSha256(directory), before);
  assert.equal(result.authenticatedOperations, 16);
  assert.equal(result.sseHandshake, true);
  assert.deepEqual(result.successfulGetPaths, paths);
  const trace = result.startupTrace;
  assert.equal(trace.status, 'complete');
  assert.deepEqual(trace.reasonCodes, []);
  assert.deepEqual(trace.missingStages, []);
  assert.deepEqual(trace.stages.map(stage => stage.stage), startupStages);
  assert.equal(trace.stages[0].pythonMonotonicNs, '9007199254740993000');
  assert.equal(trace.stages[0].pythonElapsedMs, 0);
  assert.equal(trace.stages[1].pythonDeltaMs, 0.25);
  assert.equal(trace.stages.at(-1).pythonElapsedMs, 3);
  assert.deepEqual(trace.lifecycle, { outcome: 'passed', spawn: 'spawned', spawnErrorCode: null,
    exitCode: 0, signalCode: null, closeCode: 0, closeSignal: null, close: 'closed' });
  checkTraceLifecycle(trace, true);
  checkCleanup(witness);
  assert.equal(fs.existsSync(path.join(witness, 'stream-closed')), true);
  const calls = fs.readFileSync(path.join(witness, 'calls.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(calls.filter(call => !call.authorized).length, 32);
});

test('old runtimes remain compatible and report missing when diagnostics are requested', async t => {
  const { directory, witness } = fixture(t);
  const result = await testStagedRuntime(directory, { ...settings, collectStartupTrace: true });
  assert.equal(result.startupTrace.status, 'missing');
  assert.deepEqual(result.startupTrace.stages, []);
  assert.deepEqual(result.startupTrace.reasonCodes, []);
  assert.deepEqual(result.startupTrace.missingStages, startupStages);
  checkTraceLifecycle(result.startupTrace, true);
  checkCleanup(witness);
});

for (const [mode, expected] of [['trace-exit', /exited before/], ['trace-hang', /startup timed out/], ['trace-signal', /exited before/]]) {
  test(`startup diagnostics retain a partial prefix for ${mode} and clean the owned process`, async t => {
    const { directory, witness } = fixture(t, mode);
    await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
      assert.match(error.message, expected);
      assert.equal(error.startupTrace.status, 'partial');
      assert.deepEqual(error.startupTrace.stages.map(stage => stage.stage), startupStages.slice(0, 2));
      assert.deepEqual(error.startupTrace.missingStages, startupStages.slice(2));
      assert.deepEqual(error.startupTrace.reasonCodes, []);
      assert.equal(error.startupTrace.hostTimings.firstHealthHeadersMs, undefined);
      assert.equal(error.startupTrace.hostTimings.firstHealthBodyCompleteMs, undefined);
      assert.equal(error.startupTrace.hostTimings.authenticatedReadyMs, undefined);
      assert.equal(error.startupTrace.lifecycle.outcome, mode === 'trace-hang' ? 'startup_timeout' : 'runtime_exit');
      assert.equal(error.startupTrace.lifecycle.exitCode, mode === 'trace-signal' ? null : mode === 'trace-exit' ? 7 : 0);
      assert.equal(error.startupTrace.lifecycle.closeCode, error.startupTrace.lifecycle.exitCode);
      assert.equal(error.startupTrace.lifecycle.signalCode, mode === 'trace-signal' ? 'SIGKILL' : null);
      assert.equal(error.startupTrace.lifecycle.closeSignal, error.startupTrace.lifecycle.signalCode);
      checkTraceLifecycle(error.startupTrace);
      assert.equal(JSON.stringify(error).includes('DO-NOT-PRINT-RUNTIME-LOG'), false);
      assert.equal(error.message.includes('DO-NOT-PRINT-RUNTIME-LOG'), false);
      return true;
    });
    checkCleanup(witness);
  });
}

test('state removal failure preserves the startup failure and partial trace without exposing exception text', async t => {
  const { directory, witness } = fixture(t, 'trace-exit');
  const remove = fs.rmSync;
  let retainedState;
  fs.rmSync = (target, options) => {
    if (typeof target === 'string' && path.basename(target).startsWith('monitor-staged-smoke-')) {
      retainedState = target;
      throw new Error('PRIVATE-CLEANUP-EXCEPTION /private/config');
    }
    return remove(target, options);
  };
  try {
    await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
      assert.match(error.message, /exited before authenticated startup completed/);
      assert.match(error.message, /Isolated smoke state could not be removed/);
      assert.equal(error.startupTrace.status, 'partial');
      assert.deepEqual(error.startupTrace.stages.map(stage => stage.stage), startupStages.slice(0, 2));
      assert.deepEqual(error.startupTrace.reasonCodes, []);
      checkTraceLifecycle(error.startupTrace);
      for (const privateText of ['PRIVATE-CLEANUP-EXCEPTION', '/private/config']) {
        assert.equal(error.message.includes(privateText), false);
        assert.equal(JSON.stringify(error).includes(privateText), false);
      }
      return true;
    });
    assert.equal(typeof retainedState, 'string');
    assert.equal(fs.existsSync(retainedState), true, 'injected removal failure must leave isolated state');
    const details = JSON.parse(fs.readFileSync(path.join(witness, 'process.json')));
    assert.equal(exists(details.pid), false, 'process cleanup still completes before state removal fails');
    assert.equal(path.dirname(details.home), retainedState, 'remove only this fixture owned state');
  } finally {
    fs.rmSync = remove;
    if (retainedState) remove(retainedState, { recursive: true, force: true });
  }
  checkCleanup(witness);
});

test('abort retains collected startup stages and the interruption error after cleanup', async t => {
  const { directory, witness } = fixture(t, 'trace-hang');
  const controller = new AbortController();
  const run = testStagedRuntime(directory, { ...settings, collectStartupTrace: true, signal: controller.signal });
  const rejected = assert.rejects(run, error => {
    assert.match(error.message, /interrupted/);
    assert.equal(error.startupTrace.status, 'partial');
    assert.deepEqual(error.startupTrace.stages.map(stage => stage.stage), startupStages.slice(0, 2));
    assert.equal(error.startupTrace.hostTimings.authenticatedReadyMs, undefined);
    assert.equal(error.startupTrace.lifecycle.outcome, 'interrupted');
    checkTraceLifecycle(error.startupTrace);
    return true;
  });
  for (let tries = 0; tries < 100 && !fs.existsSync(path.join(witness, 'trace-emitted')); tries++) await delay(10);
  const emitted = fs.existsSync(path.join(witness, 'trace-emitted'));
  controller.abort();
  await rejected;
  assert.equal(emitted, true);
  checkCleanup(witness);
});

test('a successful health response remains distinct from proof-verified readiness in failure diagnostics', async t => {
  const { directory, witness } = fixture(t, 'trace-wrong-proof');
  await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
    assert.match(error.message, /spawned-process identity/);
    assert.equal(error.startupTrace.status, 'complete');
    const timings = error.startupTrace.hostTimings;
    assert.ok(Number.isFinite(timings.firstHealthHeadersMs));
    assert.ok(timings.firstSuccessfulHealthHeadersMs >= timings.firstHealthHeadersMs);
    assert.ok(timings.firstHealthBodyCompleteMs >= timings.firstSuccessfulHealthHeadersMs);
    assert.ok(timings.firstSuccessfulHealthBodyCompleteMs >= timings.firstHealthBodyCompleteMs);
    assert.equal(timings.authenticatedReadyMs, undefined);
    assert.equal(error.startupTrace.lifecycle.outcome, 'validation_failed');
    checkTraceLifecycle(error.startupTrace);
    return true;
  });
  checkCleanup(witness);
});

test('health headers arrive before a deliberately delayed body and proof readiness', async t => {
  const { directory, witness } = fixture(t, 'trace-delayed-body');
  const { startupTrace: trace } = await testStagedRuntime(directory, { ...settings, collectStartupTrace: true });
  checkTraceLifecycle(trace, true);
  const timings = trace.hostTimings;
  assert.ok(timings.firstHealthBodyCompleteMs - timings.firstHealthHeadersMs >= 40);
  assert.ok(timings.firstSuccessfulHealthBodyCompleteMs - timings.firstSuccessfulHealthHeadersMs >= 40);
  assert.ok(timings.authenticatedReadyMs >= timings.firstSuccessfulHealthBodyCompleteMs);
  assert.equal(trace.lifecycle.outcome, 'passed');
  checkCleanup(witness);
});

test('collector waits for delayed close and keeps trailing markers after real child exit', async t => {
  const { directory, witness } = fixture(t, 'trace-exit');
  let timer;
  t.after(() => clearTimeout(timer));
  mockSpawn(t, (original, ...args) => {
    const child = original(...args);
    const emit = child.emit;
    child.emit = function (event, ...values) {
      if (event !== 'close') return emit.call(this, event, ...values);
      timer = setTimeout(() => {
        const trailing = startupStages.slice(2).map((stage, offset) =>
          `[sya-startup-v1] ${stage} ${9007199254740993000n + BigInt(offset + 2) * 250000n}\n`).join('');
        child.stderr.emit('data', Buffer.from(trailing));
        emit.call(child, event, ...values);
      }, 80);
      return true;
    };
    return child;
  });
  await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
    assert.match(error.message, /exited before/);
    const trace = error.startupTrace;
    assert.equal(trace.status, 'complete');
    assert.deepEqual(trace.stages.map(stage => stage.stage), startupStages);
    assert.ok(trace.stages.at(-1).receiptMs > trace.hostTimings.exitMs);
    assert.ok(trace.hostTimings.closeMs - trace.hostTimings.exitMs >= 40);
    assert.equal(trace.lifecycle.outcome, 'runtime_exit');
    assert.equal(trace.lifecycle.exitCode, 7);
    assert.equal(trace.lifecycle.closeCode, 7);
    checkTraceLifecycle(trace);
    return true;
  });
  checkCleanup(witness);
});

test('a close timeout fails completed smoke, destroys stderr and freezes diagnostic state', async t => {
  const { directory, witness } = fixture(t, 'trace-complete');
  let child, emit, destroys = 0, destroysAtClose;
  mockSpawn(t, (original, ...args) => {
    child = original(...args);
    emit = child.emit;
    const destroy = child.stderr.destroy;
    child.stderr.destroy = function (...values) { destroys += 1; return destroy.apply(this, values); };
    child.emit = function (event, ...values) {
      if (event === 'close') { destroysAtClose = destroys; return true; }
      return emit.call(this, event, ...values);
    };
    return child;
  });
  await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
    assert.match(error.message, /stdio did not close within the cleanup deadline/);
    const trace = error.startupTrace;
    assert.equal(trace.status, 'complete');
    assert.equal(trace.finished, true);
    assert.equal(trace.lifecycle.outcome, 'cleanup_failed');
    assert.equal(trace.lifecycle.close, 'timeout');
    assert.equal(trace.lifecycle.exitCode, 0);
    assert.equal(trace.lifecycle.closeCode, null);
    assert.equal(trace.hostTimings.closeMs, undefined);
    assert.ok(Number.isFinite(trace.hostTimings.authenticatedReadyMs));
    assert.ok(destroys > destroysAtClose);
    assert.equal(child.stderr.destroyed, true);
    const snapshot = JSON.stringify(trace);
    emit.call(child, 'close', 0, null);
    assert.equal(JSON.stringify(trace), snapshot, 'late close cannot rewrite returned evidence');
    return true;
  });
  checkCleanup(witness);
});

for (const style of ['sync', 'async']) {
  for (const code of ['ENOENT', 'PRIVATE-SPAWN-CODE']) {
    test(`${style} spawn failure keeps only sanitized lifecycle codes (${code === 'ENOENT' ? 'known' : 'unknown'})`, async t => {
      const { directory, witness } = fixture(t);
      let state;
      mockSpawn(t, (_original, _file, _args, options) => {
        state = path.dirname(options.env.HOME);
        const error = Object.assign(new Error('PRIVATE-SPAWN-DETAIL /private/config'), { code });
        if (style === 'sync') throw error;
        const child = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null, stderr: new PassThrough() });
        setImmediate(() => {
          child.emit('error', error);
          child.stderr.end();
          child.emit('close', -2, null);
        });
        return child;
      });
      await assert.rejects(testStagedRuntime(directory, { ...settings, collectStartupTrace: true }), error => {
        const trace = error.startupTrace;
        assert.equal(trace.status, 'missing');
        assert.equal(trace.finished, true);
        assert.equal(trace.lifecycle.outcome, 'spawn_error');
        assert.equal(trace.lifecycle.spawn, 'error');
        assert.equal(trace.lifecycle.spawnErrorCode, code === 'ENOENT' ? 'ENOENT' : 'OTHER');
        assert.equal(trace.lifecycle.exitCode, null);
        assert.equal(trace.lifecycle.signalCode, null);
        assert.equal(trace.lifecycle.close, style === 'sync' ? 'not_started' : 'closed');
        assert.equal(trace.lifecycle.closeCode, style === 'sync' ? null : -2);
        assert.ok(Number.isFinite(trace.hostTimings.errorMs));
        assert.equal(trace.hostTimings.spawnEventMs, undefined);
        assert.equal(trace.hostTimings.authenticatedReadyMs, undefined);
        assert.equal((error.message + JSON.stringify(trace)).includes('PRIVATE'), false);
        assert.equal((error.message + JSON.stringify(trace)).includes('/private'), false);
        return true;
      });
      assert.equal(fs.existsSync(state), false);
      assert.equal(fs.existsSync(path.join(witness, 'process.json')), false);
    });
  }
}

test('malformed and oversize startup markers remain bounded invalid diagnostics without private logs', async t => {
  const { directory, witness } = fixture(t, 'trace-malformed');
  const result = await testStagedRuntime(directory, { ...settings, collectStartupTrace: true });
  const trace = result.startupTrace;
  assert.equal(result.sseHandshake, true);
  assert.equal(trace.status, 'invalid');
  assert.equal(trace.stages.length, startupStages.length);
  assert.deepEqual(trace.reasonCodes, ['unknown_stage', 'marker_too_long']);
  const serialized = JSON.stringify(trace);
  assert.ok(serialized.length < 4000);
  for (const secret of ['PRIVATE-STARTUP-LOG', '/private/runtime/config', 'private_stage']) assert.equal(serialized.includes(secret), false);
  checkTraceLifecycle(trace, true);
  checkCleanup(witness);
});

test('stage smoke proves identity, denies every method, cancels SSE, preserves bytes and isolates environment', async t => {
  const { directory, witness } = fixture(t);
  const before = runtimeTreeSha256(directory);
  const prior = process.env.STAGED_SMOKE_SECRET;
  process.env.STAGED_SMOKE_SECRET = 'never-inherit-this';
  let result;
  try { result = await testStagedRuntime(directory, settings); }
  finally { if (prior === undefined) delete process.env.STAGED_SMOKE_SECRET; else process.env.STAGED_SMOKE_SECRET = prior; }
  assert.equal(result.runtimeSha256, before); assert.equal(runtimeTreeSha256(directory), before);
  assert.equal(result.authenticatedOperations, 16); assert.deepEqual(result.successfulGetPaths, paths);
  assert.equal(Object.hasOwn(result, 'startupTrace'), false, 'diagnostics remain opt-in');
  const details = checkCleanup(witness);
  assert.equal(details.envKeys.includes('STAGED_SMOKE_SECRET'), false);
  const unexpectedKeys = details.envKeys.filter(key => !['PATH', 'MONITOR_PYTHON', 'HOME', 'MONITOR_CONFIG_PATH', 'MONITOR_RECORDS_DIR', 'TMPDIR', 'TMP', 'TEMP', 'PORT', 'FUNCTION_RUNTIME_TOKEN', 'FUNCTION_RUNTIME_PROOF_KEY', 'PYTHONDONTWRITEBYTECODE'].includes(key));
  // CoreFoundation may insert this non-secret locale key inside Node on macOS;
  // it is not inherited by the smoke launcher.
  assert.deepEqual(unexpectedKeys, process.platform === 'darwin' ? ['__CF_USER_TEXT_ENCODING'] : []);
  assert.equal(details.tokenLength, 64); assert.equal(details.proofLength, 64); assert.equal(details.independentSecrets, true); assert.equal(details.bytecode, '1');
  assert.equal(details.configContents, '{}');
  assert.equal(path.dirname(details.home), path.dirname(details.config));
  assert.equal(path.dirname(details.home), path.dirname(details.records));
  assert.equal(fs.existsSync(path.join(witness, 'unsafe-success-probe')), false);
  assert.equal(fs.existsSync(path.join(witness, 'stream-closed')), true);
  const calls = fs.readFileSync(path.join(witness, 'calls.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(calls.filter(call => !call.authorized).length, 32);
  assert.deepEqual(calls.filter(call => call.authorized).map(call => call.url), [...paths, '/api/monitor/events']);
  assert(calls.some(call => call.method === 'OPTIONS' && call.url === '/api/monitor/synthetic/auth-probe?key=auth-probe'));
});

for (const [mode, expected] of [
  ['missing-proof', /spawned-process identity/], ['wrong-proof', /spawned-process identity/],
  ['auth-bypass', /authentication bypass: POST/], ['sse-bypass', /authentication bypass: GET/],
  ['mutate', /tree changed/], ['mutate-stop', /tree changed/], ['wrong-identity', /health identity/],
  ['bad-envelope', /Invalid response envelope/], ['bad-startup', /exited before/], ['hang-startup', /startup timed out/],
  ['bad-actions-object', /Invalid action discovery payload/], ['bad-actions-empty', /Invalid action discovery payload/],
  ['bad-actions-missing-safe', /missing a required safe GET/],
  ...['bad-actions-id', 'bad-actions-method', 'bad-actions-path', 'bad-actions-duplicate'].map(mode => [mode, /differs from staged OpenAPI/]),
  ...['bad-screen-empty', 'bad-screen-boolean', 'bad-screen-rate-type', 'bad-screen-rate-zero', 'bad-screen-rate-negative', 'bad-screen-rate-infinite'].map(mode => [mode, /Invalid screen status payload/]),
  ...['bad-frame-empty', 'bad-frame-dimensions'].map(mode => [mode, /Invalid latest frame payload/]),
]) {
  test(`stage smoke rejects ${mode} and cleans the owned process/state`, async t => {
    const { directory, witness } = fixture(t, mode);
    await assert.rejects(testStagedRuntime(directory, settings), error => {
      assert.match(error.message, expected);
      assert(!error.message.includes('Unable to'), error.message);
      return true;
    });
    checkCleanup(witness);
  });
}

test('latest frame accepts a valid synthetic payload as well as the normal null state', async t => {
  const { directory, witness } = fixture(t, 'frame-present');
  await testStagedRuntime(directory, settings);
  checkCleanup(witness);
});

test('a real sibling launcher is rejected before any process starts', async t => {
  const { directory, witness } = fixture(t);
  const started = path.join(witness, 'outside-started');
  const launcher = path.join(path.dirname(directory), 'outside-launcher');
  fs.writeFileSync(launcher, `#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(started)}, 'started');\n`, { mode: 0o700 });
  const manifestPath = path.join(directory, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  manifest.entry.command = '../outside-launcher';
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  await assert.rejects(testStagedRuntime(directory, settings), /Runtime entry must be a contained regular file/);
  assert.equal(fs.existsSync(started), false);
  assert.equal(fs.existsSync(path.join(witness, 'process.json')), false);
});

test('termination escalates for a resistant runtime and its actual child', async t => {
  const { directory, witness } = fixture(t, 'resist');
  await testStagedRuntime(directory, { ...settings, stopTimeoutMs: 1000 });
  checkCleanup(witness);
  assert.equal(exists(Number(fs.readFileSync(path.join(witness, 'worker-pid'), 'utf8'))), false);
});

test('transient Darwin zombie EPERM is polled until child exit and group ESRCH', async t => {
  const { directory, witness } = fixture(t);
  const kill = process.kill.bind(process);
  let stoppedGroup; let deniedProbes = 0;
  process.kill = (pid, signal) => {
    if (signal === 'SIGTERM' && pid < -1) stoppedGroup = pid;
    if (pid === stoppedGroup && signal === 0 && deniedProbes < 2) {
      deniedProbes++;
      throw Object.assign(new Error('Synthetic zombie'), { code: 'EPERM' });
    }
    return kill(pid, signal);
  };
  try { await testStagedRuntime(directory, { ...settings, stopTimeoutMs: 500 }); }
  finally { process.kill = kill; }
  assert.equal(deniedProbes, 2); checkCleanup(witness);
});

test('persistent process-group EPERM remains a cleanup failure even after child exit', async t => {
  const { directory, witness } = fixture(t);
  const kill = process.kill.bind(process); let stoppedGroup;
  process.kill = (pid, signal) => {
    if (signal === 'SIGTERM' && pid < -1) stoppedGroup = pid;
    if (pid === stoppedGroup && signal === 0) throw Object.assign(new Error('Synthetic permission failure'), { code: 'EPERM' });
    return kill(pid, signal);
  };
  try { await assert.rejects(testStagedRuntime(directory, settings), /Unable to inspect owned runtime process group \(EPERM\)/); }
  finally { process.kill = kill; }
  checkCleanup(witness);
});

test('real Python HTTP fixture exits and its process group disappears', async t => {
  const { directory, witness } = fixture(t);
  fs.writeFileSync(path.join(directory, 'bin/monitor runtime'), `#!/usr/bin/env python3
import hashlib, hmac, http.server, json, os, socketserver, sys
witness = sys.argv[2]
with open(os.path.join(witness, 'process.json'), 'w') as output:
    json.dump({'pid': os.getpid(), 'home': os.environ['HOME']}, output)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        authorized = self.headers.get('Authorization') == 'Bearer ' + os.environ['FUNCTION_RUNTIME_TOKEN']
        if not authorized:
            self.send_response(401); self.end_headers(); return
        self.send_response(200)
        if self.path == '/api/monitor/events':
            self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(b'event: ready\\ndata: {}\\n\\n'); return
        self.send_header('Content-Type', 'application/json')
        data = None
        if self.path == '/health':
            self.send_header('X-SYA-Runtime-Proof', hmac.new(os.environ['FUNCTION_RUNTIME_PROOF_KEY'].encode(), b'sya-runtime-v1', hashlib.sha256).hexdigest())
            data = {'status': 'ok', 'functionId': 'monitor', 'version': sys.argv[3]}
        elif self.path == '/manifest':
            data = {'id': 'monitor', 'version': sys.argv[3], 'runtime': 'local-http'}
        elif self.path == '/api/monitor/actions':
            with open('api.openapi.json') as contract:
                paths = json.load(contract)['paths']
            data = [{'action': operation['operationId'], 'method': method.upper(), 'path': route}
                    for route, item in paths.items() if route != '/api/monitor/events'
                    for method, operation in item.items()]
        elif self.path == '/api/monitor/screen/status':
            data = {'isMonitoring': False, 'captureRateMs': 10000}
        self.end_headers(); self.wfile.write(json.dumps({'ok': True, 'data': data}).encode())
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_HEAD = do_GET
    do_OPTIONS = do_GET
# TCPServer binding avoids reverse-DNS lookup for this synthetic loopback test.
with socketserver.TCPServer(('127.0.0.1', int(os.environ['PORT'])), Handler) as server:
    server.serve_forever()
`, { mode: 0o700 });
  await testStagedRuntime(directory, { ...settings, stopTimeoutMs: 1000 });
  checkCleanup(witness);
});

test('abort during startup still cleans the owned runtime', async t => {
  const { directory, witness } = fixture(t, 'hang-startup');
  const controller = new AbortController();
  const run = testStagedRuntime(directory, { ...settings, signal: controller.signal });
  for (let tries = 0; tries < 40 && !fs.existsSync(path.join(witness, 'process.json')); tries++) await delay(10);
  controller.abort();
  await assert.rejects(run, /interrupted/); checkCleanup(witness);
});

test('CLI returns failure without copying private runtime logs', async t => {
  const { directory, witness } = fixture(t, 'bad-startup');
  const child = spawn(process.execPath, [cli, directory], { stdio: ['ignore', 'pipe', 'pipe'] });
  let output = ''; child.stdout.on('data', data => { output += data; }); child.stderr.on('data', data => { output += data; });
  const status = await new Promise(resolve => child.once('close', resolve));
  assert.equal(status, 1); assert.match(output, /exited before/); assert(!output.includes('DO-NOT-PRINT-RUNTIME-LOG'));
  checkCleanup(witness);
});
