import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createStartupDiagnosticsCollector, startupDiagnosticsCoverage } from '../scripts/startup-diagnostics.mjs';
const make = () => createStartupDiagnosticsCollector({ nowMs: () => 12, mode: 'bootloader-importtime' });
test('default off and invalid modes', () => {
  const collector = createStartupDiagnosticsCollector({ nowMs: () => 0 });
  collector.push('[PYI-1:DEBUG] PyInstaller Bootloader 6.x\n');
  assert.equal(collector.finish().status, 'off');
  assert.throws(() => createStartupDiagnosticsCollector({ nowMs: () => 0, mode: 'debug' }));
});
test('split parent extraction, child interpreter, fixed hooks and import timings; no private text', () => {
  const collector = make();
  const lines = '[PYI-100:DEBUG] LOADER: extracting files to temporary directory...\n' +
    '[PYI-100:DEBUG] LOADER: starting the child process...\n' +
    '[PYI-101:DEBUG] LOADER: process level = 1\n' +
    '[PYI-101:DEBUG] LOADER: starting embedded python interpreter...\n' +
    '[PYI-101:DEBUG] LOADER: running pyi_rth_inspect.py\n' +
    'import time:  125 | 2000 |   ssl\n' +
    'import time: 1 | 2 | private_module_LEAK_SENTINEL\n' +
    '[PYI-101:DEBUG] LOADER: argv[0]: /secret/LEAK_SENTINEL\n';
  for (const byte of Buffer.from(lines)) collector.push(Uint8Array.of(byte));
  const result = collector.finish();
  assert.equal(result.events.length, 6);
  assert.equal(result.events[0].pid, 100);
  assert.equal(result.events[5].pid, 101);
  assert.equal(result.events[5].selfUs, 125);
  assert.equal(result.events[5].cumulativeUs, 2000);
  assert.equal(result.cumulativeTimesAdditive, false);
  assert.equal(result.clock, 'host-monotonic-receipt-ms');
  assert(!JSON.stringify(result).includes('LEAK_SENTINEL'));
});
test('line, records, total bytes, truncated and unknown data stay bounded while draining', () => {
  const collector = make();
  collector.push('LEAK_SENTINEL'.repeat(1000) + '\n');
  for (let i = 0; i < 8200; i++) collector.push('[PYI-1:DEBUG] PyInstaller Bootloader 6.x\n');
  collector.push('x'.repeat(4 * 1024 * 1024));
  const result = collector.finish();
  assert.equal(result.events.length, 8192);
  assert.equal(result.status, 'incomplete');
  assert(result.reasonCodes.includes('line_limit'));
  assert(result.reasonCodes.includes('record_limit'));
  assert(result.reasonCodes.includes('byte_limit'));
  assert(result.droppedBytes > 0);
  assert(JSON.stringify(result).length < 4 * 1024 * 1024);
  assert(!JSON.stringify(result).includes('LEAK_SENTINEL'));
  const truncated = make(); truncated.push('[PYI-1:DEBUG]');
  assert(truncated.finish().reasonCodes.includes('truncated_line'));
});
test('toString bootstrap needs no imports or external variables', () => {
  const factory = vm.runInNewContext('(' + createStartupDiagnosticsCollector.toString() + ')');
  const collector = factory({ nowMs: () => 1, mode: 'bootloader-importtime' });
  collector.push(Buffer.from('import time: 2 | 3 | ssl\n'));
  assert.equal(collector.finish().events[0].module, 'ssl');
});

test('interleaved interpreters invalidate earlier and later inferred import PID', () => {
  const collector = make();
  collector.push('[PYI-101:DEBUG] LOADER: process level = 1\nimport time: 1 | 2 | ssl\n');
  assert.equal(collector.snapshot().events[1].pidAttribution, 'inferred_single_interpreter');
  collector.push('[PYI-102:DEBUG] LOADER: process level = 2\nimport time: 1 | 2 | ssl\n');
  const result = collector.finish();
  for (const event of result.events.filter(e => e.kind === 'import')) {
    assert.equal(event.pid, null); assert.equal(event.pidAttribution, 'ambiguous');
  }
  assert(result.reasonCodes.includes('ambiguous_import_pid'));
});

const fullTrace = [
  '[PYI-100:DEBUG] LOADER: process level = 0',
  '[PYI-100:DEBUG] LOADER: this is parent process of onefile application.',
  '[PYI-100:DEBUG] LOADER: extracting files to temporary directory...',
  '[PYI-100:DEBUG] LOADER: starting the child process...',
  '[PYI-101:DEBUG] LOADER: process level = 1',
  '[PYI-101:DEBUG] LOADER: this is child process of onefile application (main application process).',
  '[PYI-101:DEBUG] LOADER: pre-initializing embedded python interpreter...',
  '[PYI-101:DEBUG] LOADER: starting embedded python interpreter...',
  'import time: 1 | 2 | ssl',
  '[PYI-101:DEBUG] LOADER: setting sys._MEIPASS',
  ...['startup_hook', 'pyi_rth_inspect', 'pyi_rth_pkgutil', 'pyi_rth_mplconfig', 'pyi_rth_multiprocessing', 'native_entry'].map(hook => `[PYI-101:DEBUG] LOADER: running ${hook}.py`),
];
test('shared coverage rejects missing debug, importtime, hooks, truncation and incorrect parent association', () => {
  const resultFor = lines => { const collector = make(); collector.push(lines.join('\n') + '\n'); return collector.finish(); };
  const options = { mode: 'bootloader-importtime', expectedParentPid: 100 };
  assert.equal(startupDiagnosticsCoverage(resultFor(fullTrace), options).ok, true);
  for (const remove of ['import time', 'extracting files', 'pre-initializing', 'pyi_rth_pkgutil']) {
    assert.equal(startupDiagnosticsCoverage(resultFor(fullTrace.filter(line => !line.includes(remove))), options).ok, false);
  }
  assert.equal(startupDiagnosticsCoverage(resultFor([]), options).ok, false);
  assert.equal(startupDiagnosticsCoverage(resultFor(fullTrace), { ...options, expectedParentPid: 999 }).ok, false);
  const unfinished = resultFor(fullTrace); unfinished.finished = false;
  assert.equal(startupDiagnosticsCoverage(unfinished, options).ok, false);
  assert.equal(startupDiagnosticsCoverage(undefined, { mode: 'off' }).ok, true);
});
