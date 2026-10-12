import { spawn } from 'node:child_process';
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { runtimeTreeSha256 } from './hash-runtime.mjs';

const METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'];
const PATH_FIELDS = ['$ref', 'summary', 'description', 'servers', 'parameters'];
const SAFE_GETS = ['/health', '/manifest', '/api/monitor/actions', '/api/monitor/screen/status', '/api/monitor/realsense/frame/latest'];
const SSE_PATH = '/api/monitor/events';
const MAX_RESPONSE_BYTES = 1024 * 1024;

function contract(runtimeDir) {
  if (!fs.lstatSync(runtimeDir).isDirectory() || fs.lstatSync(runtimeDir).isSymbolicLink()) throw new Error('Stage must be a regular directory.');
  if (fs.existsSync(path.join(runtimeDir, 'artifact-provenance.json'))) throw new Error('Producer stage must not contain consumer provenance.');
  const digest = runtimeTreeSha256(runtimeDir);
  const manifest = JSON.parse(fs.readFileSync(path.join(runtimeDir, 'manifest.json'), 'utf8'));
  const openapi = JSON.parse(fs.readFileSync(path.join(runtimeDir, 'api.openapi.json'), 'utf8'));
  if (manifest.id !== 'monitor' || manifest.runtime !== 'local-http' || typeof manifest.version !== 'string' || !manifest.version ||
      openapi.info?.version !== manifest.version || manifest.api?.health?.method !== 'GET' || manifest.api.health.path !== '/health' ||
      manifest.api?.events?.transport !== 'sse' || manifest.api.events.path !== SSE_PATH) throw new Error('Invalid staged Monitor identity or HTTP contract.');
  const command = manifest.entry?.command;
  const args = manifest.entry?.args ?? [];
  if (typeof command !== 'string' || !command || path.isAbsolute(command) || !Array.isArray(args) || args.some(arg => typeof arg !== 'string')) throw new Error('Invalid runtime entry.');
  const executable = path.resolve(runtimeDir, command);
  const relative = path.relative(runtimeDir, executable);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) || !fs.lstatSync(executable).isFile()) throw new Error('Runtime entry must be a contained regular file.');
  fs.accessSync(executable, fs.constants.X_OK);
  const probes = [];
  const ids = new Set();
  for (const [route, item] of Object.entries(openapi.paths ?? {})) {
    if (!route.startsWith('/') || route.startsWith('//') || /[\\?#\x00-\x20]/.test(route) || !item || typeof item !== 'object' || Array.isArray(item) || item.$ref) throw new Error('Invalid OpenAPI route.');
    for (const key of Object.keys(item)) {
      if (!METHODS.includes(key) && !PATH_FIELDS.includes(key) && !key.startsWith('x-')) throw new Error('Unsupported declared OpenAPI method.');
    }
    for (const method of METHODS) {
      const operation = item[method];
      if (!operation) continue;
      if (typeof operation.operationId !== 'string' || !operation.operationId || ids.has(operation.operationId)) throw new Error('Invalid or duplicate OpenAPI operation ID.');
      ids.add(operation.operationId);
      const probePath = route.replaceAll(/\{[^}]+\}/g, 'auth-probe');
      if (new URL(probePath, 'http://127.0.0.1').pathname !== probePath) throw new Error('Unsafe OpenAPI request path.');
      const query = new URLSearchParams();
      for (const parameter of [...(item.parameters ?? []), ...(operation.parameters ?? [])]) {
        if (parameter?.in === 'query' && parameter.required && typeof parameter.name === 'string') query.set(parameter.name, 'auth-probe');
      }
      probes.push({ method: method.toUpperCase(), path: `${probePath}${query.size ? `?${query}` : ''}` });
    }
  }
  for (const route of SAFE_GETS) {
    if (!openapi.paths?.[route]?.get?.responses?.['200']?.content?.['application/json']) throw new Error('Required safe GET is absent from the contract.');
  }
  if (!openapi.paths?.[SSE_PATH]?.get?.responses?.['200']?.content?.['text/event-stream']) throw new Error('Required SSE contract is absent.');
  return { manifest, openapi, executable, args, probes, digest };
}

async function freePort() {
  const server = net.createServer();
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

// Fixed loopback destination, no redirects, bounded JSON, and immediate stream
// cancellation. Never include response bodies or runtime logs in diagnostics.
function request(port, probe, token, { timeoutMs, signal, headersOnly = false }) {
  return new Promise((resolve, reject) => {
    const headers = { Accept: headersOnly ? '*/*' : 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    const body = ['GET', 'HEAD'].includes(probe.method) ? undefined : '{}';
    if (body) headers['Content-Type'] = 'application/json';
    const requestSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs);
    const req = http.request({ hostname: '127.0.0.1', port, path: probe.path, method: probe.method, headers, signal: requestSignal }, res => {
      const result = { status: res.statusCode, headers: res.headers };
      if (headersOnly) { resolve(result); res.destroy(); req.destroy(); return; }
      const chunks = []; let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > MAX_RESPONSE_BYTES) { reject(new Error('Runtime response exceeds the size limit.')); res.destroy(); req.destroy(); }
        else chunks.push(chunk);
      });
      res.once('end', () => resolve({ ...result, body: Buffer.concat(chunks).toString('utf8') }));
      res.once('error', () => reject(new Error('Runtime response failed.')));
      res.once('aborted', () => reject(new Error('Runtime response was interrupted.')));
    });
    req.once('error', () => reject(new Error('Runtime request failed or timed out.')));
    req.end(body);
  });
}

function jsonResponse(response, route, manifest, openapi) {
  if (response.status !== 200 || !String(response.headers['content-type']).includes('application/json')) throw new Error(`Safe GET failed: ${route}.`);
  let body;
  try { body = JSON.parse(response.body); } catch { throw new Error(`Invalid JSON from safe GET: ${route}.`); }
  if (route === '/manifest') {
    const value = body?.ok === true ? body.data : body;
    if (value?.id !== manifest.id || value?.version !== manifest.version || value?.runtime !== 'local-http') throw new Error('Runtime manifest identity differs from staged manifest.');
  } else {
    if (body?.ok !== true || !Object.hasOwn(body, 'data')) throw new Error(`Invalid response envelope: ${route}.`);
    if (route === '/health' && (body.data?.functionId !== manifest.id || body.data?.version !== manifest.version || body.data?.status !== 'ok')) throw new Error('Runtime health identity differs from staged manifest.');
    if (route === '/api/monitor/actions') {
      if (!Array.isArray(body.data) || body.data.length === 0) throw new Error('Invalid action discovery payload.');
      const discovered = new Set();
      for (const entry of body.data) {
        if (typeof entry?.action !== 'string' || !entry.action || discovered.has(entry.action) ||
            typeof entry.method !== 'string' || entry.method !== entry.method.toUpperCase() ||
            !METHODS.includes(entry.method.toLowerCase()) || typeof entry.path !== 'string' ||
            openapi.paths?.[entry.path]?.[entry.method.toLowerCase()]?.operationId !== entry.action) {
          throw new Error('Action discovery differs from staged OpenAPI.');
        }
        discovered.add(entry.action);
      }
      if (SAFE_GETS.some(safe => !body.data.some(entry => entry.method === 'GET' && entry.path === safe))) {
        throw new Error('Action discovery is missing a required safe GET.');
      }
    }
    if (route === '/api/monitor/screen/status' && (typeof body.data?.isMonitoring !== 'boolean' ||
        !Number.isFinite(body.data?.captureRateMs) || body.data.captureRateMs <= 0)) {
      throw new Error('Invalid screen status payload.');
    }
    if (route === '/api/monitor/realsense/frame/latest' && body.data !== null) {
      const frame = body.data;
      if (frame?.source !== 'realsense' || typeof frame.frameId !== 'string' || !frame.frameId ||
          !Number.isInteger(frame.timestamp) || typeof frame.capturedAt !== 'string' || !Number.isFinite(Date.parse(frame.capturedAt)) ||
          !Number.isInteger(frame.frameNumber) || !Number.isInteger(frame.width) || frame.width < 1 ||
          !Number.isInteger(frame.height) || frame.height < 1 || frame.mimeType !== 'image/jpeg' || typeof frame.jpegBase64 !== 'string' ||
          typeof frame.depth?.available !== 'boolean' || !Number.isInteger(frame.depth.width) || frame.depth.width < 0 ||
          !Number.isInteger(frame.depth.height) || frame.depth.height < 0 || typeof frame.depth.format !== 'string' ||
          !(frame.depth.depthScaleMeters === null || Number.isFinite(frame.depth.depthScaleMeters)) ||
          !(frame.attention === null || (frame.attention && typeof frame.attention === 'object' && !Array.isArray(frame.attention)))) {
        throw new Error('Invalid latest frame payload.');
      }
    }
  }
}

function groupExists(pid) {
  try { process.kill(-pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw new Error(`Unable to inspect owned runtime process group (${error.code || 'unknown'}).`); }
}

async function stopRuntime(child, timeoutMs) {
  if (!child?.pid) return;
  let inspectError;
  const signal = value => {
    try { process.kill(-child.pid, value); }
    catch (error) { if (error.code !== 'ESRCH') throw new Error(`Unable to stop owned runtime process group (${error.code || 'unknown'}).`); }
  };
  const wait = async () => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        if (!groupExists(child.pid) && (child.exitCode !== null || child.signalCode !== null)) return true;
      } catch (error) {
        // Darwin can report EPERM while a terminated child is still a zombie.
        // Keep waiting for both the child exit and an explicit ESRCH result.
        inspectError = error;
      }
      await delay(25);
    }
    return false;
  };
  signal('SIGTERM');
  if (await wait()) return;
  signal('SIGKILL');
  if (!(await wait())) throw inspectError ?? new Error('Owned runtime process group did not exit.');
}

export async function testStagedRuntime(directory, { startupTimeoutMs = 10000, requestTimeoutMs = 1500, stopTimeoutMs = 2000, signal } = {}) {
  if (process.platform === 'win32') throw new Error('Staged runtime smoke currently requires a POSIX runner.');
  for (const value of [startupTimeoutMs, requestTimeoutMs, stopTimeoutMs]) if (!Number.isSafeInteger(value) || value < 1 || value > 30000) throw new Error('Invalid smoke timeout.');
  const runtimeDir = path.resolve(directory);
  const { manifest, openapi, executable, args, probes, digest } = contract(runtimeDir);
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-staged-smoke-'));
  let child; let failure; let spawnFailed = false;
  try {
    const home = path.join(state, 'home'), records = path.join(state, 'records'), temporary = path.join(state, 'tmp');
    for (const dir of [home, records, temporary]) fs.mkdirSync(dir, { mode: 0o700 });
    const configPath = path.join(state, 'monitor-config.json');
    fs.writeFileSync(configPath, '{}', { mode: 0o600 });
    const token = randomBytes(32).toString('hex'), proofKey = randomBytes(32).toString('hex');
    const wrongToken = randomBytes(32).toString('hex');
    const expectedProof = createHmac('sha256', proofKey).update('sya-runtime-v1').digest();
    const port = await freePort();
    signal?.throwIfAborted();
    child = spawn(executable, args, {
      cwd: runtimeDir, detached: true, shell: false, stdio: 'ignore',
      env: {
        ...(process.env.PATH ? { PATH: process.env.PATH } : {}),
        ...(process.env.MONITOR_PYTHON ? { MONITOR_PYTHON: process.env.MONITOR_PYTHON } : {}),
        HOME: home, MONITOR_CONFIG_PATH: configPath, MONITOR_RECORDS_DIR: records,
        TMPDIR: temporary, TMP: temporary, TEMP: temporary,
        PORT: String(port), FUNCTION_RUNTIME_TOKEN: token, FUNCTION_RUNTIME_PROOF_KEY: proofKey, PYTHONDONTWRITEBYTECODE: '1',
      },
    });
    child.once('error', () => { spawnFailed = true; });
    const deadline = Date.now() + startupTimeoutMs;
    let healthy = false;
    while (Date.now() < deadline) {
      signal?.throwIfAborted();
      if (spawnFailed || child.exitCode !== null || child.signalCode !== null) throw new Error('Runtime exited before authenticated startup completed.');
      let response;
      try { response = await request(port, { method: 'GET', path: '/health' }, token, { timeoutMs: Math.max(1, Math.min(requestTimeoutMs, deadline - Date.now())), signal }); }
      catch { await delay(25); continue; }
      if (response.status !== 200) { await delay(25); continue; }
      const proof = response.headers['x-sya-runtime-proof'];
      if (typeof proof !== 'string' || !/^[a-f0-9]{64}$/.test(proof) || !timingSafeEqual(Buffer.from(proof, 'hex'), expectedProof)) throw new Error('Runtime health did not prove spawned-process identity.');
      jsonResponse(response, '/health', manifest, openapi); healthy = true; break;
    }
    if (!healthy) throw new Error('Runtime startup timed out.');
    for (const probe of probes) {
      for (const authorization of [undefined, wrongToken]) {
        const response = await request(port, probe, authorization, { timeoutMs: requestTimeoutMs, signal, headersOnly: true });
        if (![401, 403].includes(response.status)) throw new Error(`Runtime authentication bypass: ${probe.method} ${probe.path}.`);
        if (response.headers['x-sya-runtime-proof']) throw new Error('Unauthenticated runtime response exposed a process proof.');
      }
    }
    for (const route of SAFE_GETS.slice(1)) {
      jsonResponse(await request(port, { method: 'GET', path: route }, token, { timeoutMs: requestTimeoutMs, signal }), route, manifest, openapi);
    }
    const stream = await request(port, { method: 'GET', path: SSE_PATH }, token, { timeoutMs: requestTimeoutMs, signal, headersOnly: true });
    if (stream.status !== 200 || !String(stream.headers['content-type']).includes('text/event-stream')) throw new Error('Authenticated SSE handshake failed.');
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('Runtime exited during smoke.');
  } catch (error) { failure = signal?.aborted ? new Error('Staged runtime smoke interrupted.') : error; }
  finally {
    const errors = failure ? [failure.message] : [];
    try { await stopRuntime(child, stopTimeoutMs); } catch (error) { errors.push(error.message); }
    try {
      if (fs.existsSync(path.join(runtimeDir, 'artifact-provenance.json')) || runtimeTreeSha256(runtimeDir) !== digest) errors.push('Staged runtime tree changed during smoke.');
    } catch { errors.push('Staged runtime tree could not be verified after smoke.'); }
    fs.rmSync(state, { recursive: true, force: true });
    if (errors.length) failure = new Error(errors.join(' '));
  }
  if (failure) throw failure;
  return { runtimeSha256: digest, authenticatedOperations: probes.length, successfulGetPaths: SAFE_GETS, sseHandshake: true };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const controller = new AbortController();
  const interrupt = () => controller.abort();
  process.once('SIGINT', interrupt); process.once('SIGTERM', interrupt);
  try {
    if (process.argv.length !== 3) throw new Error('Usage: node scripts/test-staged-runtime.mjs <staged-directory>');
    console.log(JSON.stringify(await testStagedRuntime(process.argv[2], { signal: controller.signal })));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
  finally { process.removeListener('SIGINT', interrupt); process.removeListener('SIGTERM', interrupt); }
}
