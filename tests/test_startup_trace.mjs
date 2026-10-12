import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { createStartupTraceCollector } from '../scripts/startup-trace.mjs';

const stages = [
  'bootstrap', 'entry', 'entry_imports_complete', 'freeze_support_complete',
  'tls_start', 'tls_complete', 'server_import_start', 'server_import_complete',
  'config_start', 'config_complete', 'bind_start', 'bind_complete', 'loop_ready',
];
const marker = (stage, stamp = 1000000n) => `[sya-startup-v1] ${stage} ${stamp}\n`;
const fullTrace = (base = 9007199254740993000n) => stages.map((stage, index) => marker(stage, base + BigInt(index) * 1250000n)).join('');
const collector = () => createStartupTraceCollector({ nowMs: () => 2000.5 });

test('complete trace keeps Python durations and host receipt times in separate clocks', () => {
  let host = 10000.75;
  const trace = createStartupTraceCollector({ nowMs: () => host });
  const base = 9007199254740993000n;
  stages.forEach((stage, index) => {
    host += 5;
    trace.push(marker(stage, base + BigInt(index) * 1250000n));
  });
  const result = trace.finish();
  assert.equal(result.status, 'complete');
  assert.equal(result.finished, true);
  assert.deepEqual(result.reasonCodes, []);
  assert.deepEqual(result.missingStages, []);
  assert.deepEqual(result.stages.map(row => row.stage), stages);
  assert.equal(result.stages[0].pythonMonotonicNs, String(base));
  assert.equal(result.stages[0].pythonElapsedMs, 0);
  assert.equal(result.stages[0].pythonDeltaMs, 0);
  assert.equal(result.stages[1].pythonDeltaMs, 1.25);
  assert.equal(result.stages.at(-1).pythonElapsedMs, 15);
  assert.equal(result.stages.at(-1).receiptMs, host);
  assert.doesNotThrow(() => JSON.stringify(result));
});

test('line fragments and CRLF work with strings, bytes, and every single split boundary', () => {
  const input = fullTrace().replaceAll('\n', '\r\n');
  for (let cut = 0; cut <= input.length; cut += 1) {
    const trace = collector();
    trace.push(input.slice(0, cut));
    trace.push(Buffer.from(input.slice(cut)));
    assert.equal(trace.finish().status, 'complete', `split ${cut}`);
  }
  const trace = collector();
  for (const byte of Buffer.from(input)) trace.push(new Uint8Array([byte]));
  assert.equal(trace.finish().status, 'complete');
});

test('receipt time belongs to the chunk completing the marker', () => {
  let host = 25;
  const trace = createStartupTraceCollector({ nowMs: () => host });
  trace.push('[sya-startup-v1] bootstrap 123');
  host = 40;
  trace.push('\n');
  assert.equal(trace.finish().stages[0].receiptMs, 40);
});

test('old runtimes with no markers report missing and discard arbitrary stderr', () => {
  const trace = collector();
  trace.push('PRIVATE credential=/secret/file\n');
  trace.push('log prefix ' + fullTrace());
  // The first marker was embedded in a log line; the remaining standalone
  // markers are real protocol attempts but cannot replace missing bootstrap.
  assert.equal(trace.finish().status, 'invalid');
  const old = collector();
  old.push('PRIVATE credential=/secret/file\nlog [sya-startup-v1] bootstrap 1\nunterminated ordinary log');
  const result = old.finish();
  assert.equal(result.status, 'missing');
  assert.equal(result.stages.length, 0);
  assert.deepEqual(result.missingStages, stages);
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
  assert.equal(JSON.stringify(result).includes('/secret'), false);
});

test('incomplete startup retains a valid prefix, and snapshots cannot mutate it', () => {
  const trace = collector();
  trace.push(marker('bootstrap') + marker('entry', 1000001n));
  const interim = trace.snapshot();
  assert.equal(interim.status, 'partial');
  assert.equal(interim.finished, false);
  interim.stages[0].stage = 'changed';
  interim.missingStages.length = 0;
  interim.reasonCodes.push('changed');
  const result = trace.finish();
  assert.equal(result.status, 'partial');
  assert.equal(result.stages[0].stage, 'bootstrap');
  assert.deepEqual(result.missingStages, stages.slice(2));
  assert.deepEqual(result.reasonCodes, []);
  assert.deepEqual(trace.finish(), result);
  trace.push(fullTrace());
  assert.deepEqual(trace.snapshot({ final: true }), result);
});

for (const [name, input, reason] of [
  ['unknown version', '[sya-startup-v2] bootstrap 1\n', 'unsupported_version'],
  ['malformed version', '[sya-startup-vx] bootstrap 1\n', 'invalid_format'],
  ['extra field', '[sya-startup-v1] bootstrap 1 PRIVATE\n', 'invalid_format'],
  ['unknown stage', marker('private_stage'), 'unknown_stage'],
  ['negative timestamp', marker('bootstrap', '-1'), 'invalid_timestamp'],
  ['fraction timestamp', marker('bootstrap', '1.2'), 'invalid_timestamp'],
  ['scientific timestamp', marker('bootstrap', '1e9'), 'invalid_timestamp'],
  ['long timestamp', marker('bootstrap', '1'.repeat(21)), 'invalid_timestamp'],
  ['leading zeros', marker('bootstrap', '01'), 'invalid_timestamp'],
  ['non-ascii', marker('bootstrap', '秘密'), 'invalid_format'],
  ['duplicate', marker('bootstrap') + marker('bootstrap'), 'duplicate_stage'],
  ['out of order', marker('entry'), 'out_of_order_stage'],
  ['skipped stage', marker('bootstrap') + marker('entry_imports_complete'), 'out_of_order_stage'],
  ['Python clock regression', marker('bootstrap', 5n) + marker('entry', 4n), 'python_clock_regression'],
]) {
  test(`rejects ${name} without raw diagnostic contents`, () => {
    const trace = collector();
    trace.push(input);
    const result = trace.finish();
    assert.equal(result.status, 'invalid');
    assert.deepEqual(result.reasonCodes, [reason]);
    assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
    assert.equal(JSON.stringify(result).includes('private_stage'), false);
    assert.equal(JSON.stringify(result).includes('秘密'), false);
  });
}

test('equal Python timestamps and the bounded decimal endpoints are valid', () => {
  for (const stamp of [0n, 99999999999999999999n]) {
    const trace = collector();
    trace.push(stages.map(stage => marker(stage, stamp)).join(''));
    const result = trace.finish();
    assert.equal(result.status, 'complete');
    assert.ok(result.stages.every(row => row.pythonDeltaMs === 0 && row.pythonElapsedMs === 0));
  }
});

test('an invalid marker cannot be erased by a later complete sequence', () => {
  const trace = collector();
  trace.push(marker('entry'));
  trace.push(fullTrace());
  assert.equal(trace.finish().status, 'invalid');
});

test('finalization rejects an unterminated candidate but not a live fragmented line', () => {
  for (const incomplete of ['[sya-startup', '[sya-startup-v1] bootstrap 1', '[sya-startup-v1] bootstrap 1\r']) {
    const trace = collector();
    trace.push(incomplete);
    assert.equal(trace.snapshot().status, 'missing');
    const result = trace.snapshot({ final: true });
    assert.equal(result.status, 'invalid');
    assert.deepEqual(result.reasonCodes, ['truncated_marker']);
    assert.equal(result.stages.length, 0);
  }
});

test('finalization rejects every partial family prefix even after a complete trace', () => {
  for (let length = 1; length < '[sya-startup'.length; length += 1) {
    for (const completeFirst of [false, true]) {
      const trace = collector();
      if (completeFirst) trace.push(fullTrace());
      trace.push('[sya-startup'.slice(0, length));
      assert.equal(trace.snapshot().status, completeFirst ? 'complete' : 'missing');
      const result = trace.finish();
      assert.equal(result.status, 'invalid');
      assert.deepEqual(result.reasonCodes, ['truncated_marker']);
      assert.equal(result.stages.length, completeFirst ? stages.length : 0);
    }
  }
});

test('actual Python emitter and JavaScript collector share the complete wire protocol', () => {
  const script = `
import importlib.util
import sys
from unittest import mock
spec = importlib.util.spec_from_file_location("synthetic_trace", sys.argv[1])
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)
stamps = (9007199254740993000 + index * 1250000 for index in range(len(trace.STAGES)))
with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(trace.time, "monotonic_ns", side_effect=stamps):
    for stage in trace.STAGES:
        trace.emit(stage)
`;
  const emitted = spawnSync('python3', ['-I', '-B', '-c', script,
    fileURLToPath(new URL('../startup_trace.py', import.meta.url))], {
    timeout: 5000, maxBuffer: 16384, encoding: 'utf8',
  });
  assert.equal(emitted.error, undefined);
  assert.equal(emitted.status, 0, emitted.stderr);
  assert.equal(emitted.stdout, '');
  const trace = collector();
  const bytes = Buffer.from(emitted.stderr);
  for (let offset = 0; offset < bytes.length; offset += 7) trace.push(bytes.subarray(offset, offset + 7));
  const result = trace.finish();
  assert.equal(result.status, 'complete');
  assert.deepEqual(result.stages.map(row => row.stage), stages);
  assert.equal(result.stages[0].pythonMonotonicNs, '9007199254740993000');
  assert.equal(result.stages.at(-1).pythonElapsedMs, 15);
  assert.deepEqual(result.reasonCodes, []);
});

test('large noise chunks are skipped without decoding and cannot hide a following trace', () => {
  const trace = collector();
  const noise = Buffer.alloc(4 * 1024 * 1024, 120);
  noise.toString = () => { throw new Error('must not decode arbitrary stderr'); };
  trace.push(noise);
  trace.push('\n' + fullTrace());
  assert.equal(trace.finish().status, 'complete');
});

test('oversize markers and repeated invalid lines have bounded sanitized reports', () => {
  const trace = collector();
  trace.push('[sya-startup-v1] bootstrap ' + 'PRIVATE'.repeat(1024 * 1024));
  assert.deepEqual(trace.snapshot().reasonCodes, ['marker_too_long']);
  trace.push('\n');
  for (let index = 0; index < 2000; index += 1) trace.push(marker('private_stage'));
  trace.push(fullTrace());
  const result = trace.finish();
  assert.equal(result.status, 'invalid');
  assert.deepEqual(result.reasonCodes, ['marker_too_long', 'unknown_stage']);
  assert.equal(result.stages.length, 13);
  assert.ok(JSON.stringify(result).length < 3000);
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
});

test('invalid host clocks are sanitized and cannot produce stage receipts', () => {
  for (const badNow of [() => NaN, () => Infinity, () => -1, () => '1', () => { throw new Error('PRIVATE'); }]) {
    const trace = createStartupTraceCollector({ nowMs: badNow });
    trace.push(marker('bootstrap'));
    const result = trace.finish();
    assert.equal(result.status, 'invalid');
    assert.deepEqual(result.reasonCodes, ['invalid_host_clock']);
    assert.deepEqual(result.stages, []);
    assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
  }
  let time = 2;
  const trace = createStartupTraceCollector({ nowMs: () => time-- });
  trace.push(marker('bootstrap'));
  trace.push(marker('entry'));
  assert.deepEqual(trace.finish().reasonCodes, ['host_clock_regression']);
});

test('invalid input stays bounded and empty chunks do not read the clock', () => {
  const trace = createStartupTraceCollector({ nowMs: () => { throw new Error('clock should not run'); } });
  trace.push(''); trace.push(Buffer.alloc(0));
  assert.equal(trace.snapshot().status, 'missing');
  trace.push({ private: 'PRIVATE' });
  const result = trace.finish();
  assert.deepEqual(result.reasonCodes, ['invalid_chunk']);
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
  assert.throws(() => createStartupTraceCollector(), /monotonic nowMs/);
});

test('toString serialization runs in a fresh realm without imports or Node globals', () => {
  const embedded = vm.runInNewContext(`(${createStartupTraceCollector.toString()})`, {}, { timeout: 1000 });
  const trace = embedded({ nowMs: () => 7 });
  trace.push(Buffer.from(fullTrace()));
  const report = JSON.parse(JSON.stringify(trace.finish()));
  assert.equal(report.status, 'complete');
  assert.equal(report.stages.length, 13);
  assert.ok(report.stages.every(row => row.receiptMs === 7));
});
