import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-stage-recipe-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const directory of ['scripts', 'patches', 'dist', 'release/out']) fs.mkdirSync(path.join(root, directory), { recursive: true });
  for (const name of ['manifest.json', 'api.openapi.json', 'README.md', 'package.json', 'module.json', 'index.html', 'server.py', 'realsense_runtime.py', 'eye_observation.py', 'config.example.json', 'requirements-realsense.txt', 'dist/main.js', 'patches/librealsense-v2.54.2-macos-l515.patch']) {
    fs.writeFileSync(path.join(root, name), `fixture:${name}`);
  }
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ type: 'commonjs' }));
  fs.writeFileSync(path.join(root, 'scripts/realsense-l515'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const script = path.join(root, 'scripts/stage-runtime.sh');
  fs.copyFileSync(new URL('../scripts/stage-runtime.sh', import.meta.url), script);
  fs.writeFileSync(path.join(root, 'release/out/source-provenance.json'), 'external provenance');
  for (const entry of ['main', 'renderer', 'types']) for (const extension of ['js', 'd.ts']) {
    const filename = `dist/${entry}.${extension}`;
    fs.writeFileSync(path.join(root, filename), `fixture:${filename}`);
  }
  for (const args of [['init'], ['add', 'scripts', 'patches']]) {
    const result = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
  }
  return { root, script };
}

function run(script, args = [], env = process.env) {
  return spawnSync('bash', [script, ...args], { cwd: os.tmpdir(), env, encoding: 'utf8', timeout: 10000 });
}

test('shared recipe stages from its own source root and preserves external provenance', t => {
  const { root, script } = fixture(t);
  const result = run(script);
  assert.equal(result.error, undefined); assert.equal(result.status, 0, result.stderr);
  const stage = path.join(root, 'release/stage');
  assert.equal(fs.readFileSync(path.join(stage, 'assets/server.py'), 'utf8'), 'fixture:server.py');
  assert.equal(fs.readFileSync(path.join(stage, 'assets/eye_observation.py'), 'utf8'), 'fixture:eye_observation.py');
  assert.equal(fs.readFileSync(path.join(stage, 'dist/main.js'), 'utf8'), 'fixture:dist/main.js');
  fs.accessSync(path.join(stage, 'bin/monitor-server'), fs.constants.X_OK);
  assert.match(fs.readFileSync(path.join(stage, 'bin/monitor-server.cmd'), 'utf8'), /MONITOR_PYTHON/);
  assert.equal(fs.readFileSync(path.join(root, 'release/out/source-provenance.json'), 'utf8'), 'external provenance');
});

test('staged eye evaluator runs the real CLI with only its packaged module and synthetic input', t => {
  const { root, script } = fixture(t);
  for (const name of ['eye_observation.py', 'scripts/evaluate-eye-observations.py']) {
    fs.copyFileSync(new URL(`../${name}`, import.meta.url), path.join(root, name));
  }
  const tracked = spawnSync('git', ['add', 'scripts/evaluate-eye-observations.py'], { cwd: root, encoding: 'utf8' });
  assert.equal(tracked.status, 0, tracked.stderr);
  const staged = run(script);
  assert.equal(staged.error, undefined);
  assert.equal(staged.status, 0, staged.stderr);
  const stage = path.join(root, 'release/stage');
  const extractorVersion = 'mediapipe-face-mesh-0.10.21-ear-v1';
  const config = { schema_version: 1, extractor_version: extractorVersion, session_id: 'synthetic',
    duration_seconds: 2, max_samples: 20, max_gap_seconds: 1 };
  const geometry = { schema_version: 1, extractor_version: extractorVersion, width: 640, height: 480,
    left_ear: 0.2, right_ear: 0.25, left_width_px: 40, right_width_px: 40, reason: null };
  const rows = [100, 100.5].map((time, index) => ({ schema_version: 1, session_id: 'synthetic',
    color_frame_number: index + 1, captured_at_ms: time * 1000, acquired_monotonic_s: time,
    completed_monotonic_s: time + 0.05, geometry }));
  const configPath = path.join(root, 'synthetic-config.json');
  const inputPath = path.join(root, 'synthetic-observations.jsonl');
  fs.writeFileSync(configPath, JSON.stringify(config));
  fs.writeFileSync(inputPath, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
  assert.equal(fs.existsSync(path.join(stage, 'eye_observation.py')), false);
  // Isolated Python and an unrelated cwd prevent source/PYTHONPATH fallback.
  const result = spawnSync(process.env.MONITOR_PYTHON || 'python3', [
    '-I', '-B', path.join(stage, 'scripts/evaluate-eye-observations.py'),
    '--config', configPath, '--input', inputPath, '--as-of-monotonic-s', '100.5',
  ], { cwd: os.tmpdir(), encoding: 'utf8', timeout: 10000 });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, '');
  const report = JSON.parse(result.stdout);
  assert.equal(report.valid_samples, 2);
  assert.equal(report.coverage_seconds, 0.5);
  assert.equal(report.coverage_ratio, 0.25);
  assert.equal(report.valid_sample_rate_hz, 1);
});

test('shared recipe excludes untracked source and non-entry generated files', t => {
  const { root, script } = fixture(t);
  for (const filename of ['scripts/untracked-helper', 'scripts/.env', 'patches/private.log', 'dist/extra.js', 'dist/.env']) {
    fs.writeFileSync(path.join(root, filename), 'must not enter package');
  }
  const result = run(script);
  assert.equal(result.status, 0, result.stderr);
  for (const filename of ['scripts/untracked-helper', 'scripts/.env', 'patches/private.log', 'dist/extra.js', 'dist/.env']) {
    assert.equal(fs.existsSync(path.join(root, 'release/stage', filename)), false, filename);
  }
});

test('shared recipe rejects destinations inside inputs including normalized and symlink paths', t => {
  const { root, script } = fixture(t);
  fs.symlinkSync(path.join(root, 'scripts'), path.join(root, 'alias'));
  for (const relative of ['scripts/staged', 'patches/sub/../staged', 'dist/staged', 'alias/staged']) {
    const result = run(script, [path.join(root, relative)]);
    assert.notEqual(result.status, 0, relative);
    assert.match(result.stderr, /outside copied input/);
    assert.equal(fs.existsSync(path.join(root, relative)), false);
  }
});

test('shared recipe atomically refuses an output created after its existence check', t => {
  const { root, script } = fixture(t);
  const output = path.join(fs.realpathSync(root), 'raced-output');
  const shim = path.join(root, 'shim'); fs.mkdirSync(shim);
  const mkdir = spawnSync('which', ['mkdir'], { encoding: 'utf8' }).stdout.trim();
  fs.writeFileSync(path.join(shim, 'mkdir'), `#!${process.execPath}\n` + `
    const fs = require('fs'), path = require('path'), cp = require('child_process');
    const args = process.argv.slice(2), out = process.env.RACED_OUTPUT;
    if (!fs.existsSync(out) && args.some(arg => arg === out || arg.startsWith(out + '/'))) {
      fs.mkdirSync(path.join(out, 'assets'), {recursive:true});
      fs.writeFileSync(path.join(out, 'assets/server.py'), 'other invocation');
    }
    const result = cp.spawnSync(${JSON.stringify(mkdir)}, args, {stdio:'inherit'});
    process.exit(result.status ?? 1);
  `, { mode: 0o755 });
  const result = run(script, [output], { ...process.env, PATH: `${shim}:${process.env.PATH}`, RACED_OUTPUT: output });
  assert.notEqual(result.status, 0);
  assert.equal(fs.existsSync(path.join(output, 'assets/server.py')), true, result.stderr);
  assert.equal(fs.readFileSync(path.join(output, 'assets/server.py'), 'utf8'), 'other invocation');
});

test('shared recipe accepts a new path with spaces and Unicode without modifying existing output', t => {
  const { root, script } = fixture(t);
  const output = path.join(root, '候选 runtime');
  assert.equal(run(script, [output]).status, 0);
  fs.writeFileSync(path.join(output, 'keep'), 'user bytes');
  const result = run(script, [output]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /already exists/);
  assert.equal(fs.readFileSync(path.join(output, 'keep'), 'utf8'), 'user bytes');
});

test('shared recipe rejects a dangling symlink destination without creating its target', t => {
  const { root, script } = fixture(t);
  const output = path.join(root, 'linked-stage');
  const target = path.join(root, 'absent-target');
  fs.symlinkSync(target, output);
  const result = run(script, [output]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /already exists/);
  assert.equal(fs.existsSync(target), false);
  assert.equal(fs.lstatSync(output).isSymbolicLink(), true);
});
