import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { runtimeTreeSha256 } from '../scripts/hash-runtime.mjs';

const workflow = fs.readFileSync(new URL('../.github/workflows/release.yml', import.meta.url), 'utf8');
const archiveBlock = workflow.match(/      - name: Create platform archives\n        shell: bash\n        run: \|\n([\s\S]*?)\n      - name:/)?.[1];
assert(archiveBlock, 'actual archive workflow step must exist');
const script = archiveBlock.replace(/^          /gm, '').replaceAll('${{ steps.meta.outputs.version }}', '0.2.0');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-archive-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const name of ['release/stage/empty', 'release/stage/bin', 'release/out', 'scripts']) fs.mkdirSync(path.join(root, name), { recursive: true });
  fs.writeFileSync(path.join(root, 'release/stage/bin/monitor-server'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  fs.writeFileSync(path.join(root, 'release/stage/payload'), Buffer.from([0, 1, 254, 255]));
  fs.copyFileSync(new URL('../scripts/hash-runtime.mjs', import.meta.url), path.join(root, 'scripts/hash-runtime.mjs'));
  const digest = runtimeTreeSha256(path.join(root, 'release/stage'));
  fs.writeFileSync(path.join(root, 'release/smoke-result.json'), JSON.stringify({ runtimeSha256: digest }));
  return { root, digest };
}

function run(root, extraPath = '') {
  return spawnSync('bash', ['--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', script], {
    cwd: root, encoding: 'utf8', timeout: 15000,
    env: { PATH: `${extraPath ? `${extraPath}${path.delimiter}` : ''}${path.dirname(process.execPath)}${path.delimiter}${process.env.PATH}`, HOME: root },
  });
}

test('actual archive step preserves the smoke digest across all ZIP/tar assets and external metadata', t => {
  const { root, digest } = fixture(t);
  assert.match(workflow, /test-staged-runtime\.mjs release\/stage > release\/smoke-result\.json/);
  const result = run(root);
  assert.equal(result.error, undefined); assert.equal(result.status, 0, result.stderr);
  const output = path.join(root, 'release/out');
  const metadataFiles = fs.readdirSync(output).filter(name => name.endsWith('.digests.json'));
  assert.equal(metadataFiles.length, 4);
  for (const name of metadataFiles) {
    const metadata = JSON.parse(fs.readFileSync(path.join(output, name)));
    assert.equal(metadata.runtimeSha256, digest);
    const archive = fs.readFileSync(path.join(output, name.replace(/\.digests\.json$/, '')));
    assert.equal(metadata.sha256, createHash('sha256').update(archive).digest('hex'));
  }
  for (const platform of ['macos-arm64', 'macos-x64', 'windows-x64', 'linux-x64']) {
    assert.equal(runtimeTreeSha256(path.join(root, 'release/verify', platform)), digest);
  }
  assert.equal(fs.readFileSync(path.join(output, 'SHA256SUMS.txt'), 'utf8').trim().split('\n').length, 8);
});

test('actual archive step rejects a stage changed after successful smoke before creating any archive', t => {
  const { root } = fixture(t);
  fs.appendFileSync(path.join(root, 'release/stage/payload'), 'changed after smoke');
  const result = run(root);
  assert.equal(result.error, undefined); assert.notEqual(result.status, 0);
  assert.deepEqual(fs.readdirSync(path.join(root, 'release/out')), []);
});

test('actual archive step rejects an archive whose unpacked tree differs from the tested stage', t => {
  const { root, digest } = fixture(t);
  const shim = path.join(root, 'shim'); fs.mkdirSync(shim);
  const zip = spawnSync('which', ['zip'], { encoding: 'utf8' }).stdout.trim();
  assert(zip && !zip.includes("'"));
  // Alter only this test archive, then restore stage before the workflow compares it.
  fs.writeFileSync(path.join(shim, 'zip'), `#!/bin/sh\nset -e\ntouch unexpected-archive-entry\ntrap 'rm -f unexpected-archive-entry' EXIT\n'${zip}' "$@"\n`, { mode: 0o755 });
  const result = run(root, shim);
  assert.equal(result.error, undefined); assert.notEqual(result.status, 0);
  assert.equal(runtimeTreeSha256(path.join(root, 'release/stage')), digest);
  assert.notEqual(runtimeTreeSha256(path.join(root, 'release/verify/macos-arm64')), digest);
  assert.equal(fs.existsSync(path.join(root, 'release/out/SHA256SUMS.txt')), false);
});
