import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { verifyReleaseSource } from '../scripts/verify-release-source.mjs';

const repository = 'fixture/monitor';
const cli = fileURLToPath(new URL('../scripts/verify-release-source.mjs', import.meta.url));
const workflow = fs.readFileSync(new URL('../.github/workflows/release.yml', import.meta.url), 'utf8');

function fixture(t, { annotated = false } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-release-source-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const cwd = path.join(directory, 'source'); fs.mkdirSync(cwd);
  const remote = path.join(directory, 'remote.git');
  const env = { PATH: process.env.PATH, HOME: directory,
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: path.join(directory, 'no-git-config'),
    GITHUB_REPOSITORY: repository, GITHUB_SERVER_URL: 'https://github.com',
    GITHUB_EVENT_NAME: 'push', GITHUB_REF_TYPE: 'tag', GITHUB_REF_NAME: 'v0.2.0' };
  const git = (...args) => {
    const result = spawnSync('git', args, { cwd, env, encoding: 'utf8', timeout: 10000 });
    assert.equal(result.status, 0, result.stderr); return result.stdout.trim();
  };
  git('init', '--bare', remote); git('init', '-b', 'main');
  git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture');
  git('remote', 'add', 'origin', remote);
  for (const [name, value] of Object.entries({ 'manifest.json': { version: '0.2.0' },
    'package.json': { version: '0.2.0' }, 'api.openapi.json': { info: { version: '0.2.0' } } })) {
    fs.writeFileSync(path.join(cwd, name), JSON.stringify(value));
  }
  fs.writeFileSync(path.join(cwd, 'tracked'), 'original');
  git('add', '.'); git('commit', '-m', 'initial');
  if (annotated) git('tag', '-a', 'v0.2.0', '-m', 'version annotation');
  else git('tag', 'v0.2.0');
  git('push', 'origin', 'main', 'v0.2.0');
  env.GITHUB_SHA = git('rev-parse', 'HEAD');
  const state = { listing: '[[]]', status: 0, stderr: '', origin: `https://github.com/${repository}.git`, calls: [] };
  const run = (program, args, options) => {
    state.calls.push([program, ...args]);
    if (program === 'git' && args.join(' ') === 'remote get-url origin') {
      return { status: 0, stdout: state.origin, stderr: '' };
    }
    if (program === 'gh') {
      assert.deepEqual(args, ['api', '--hostname', 'github.com', '--method', 'GET',
        `repos/${repository}/releases?per_page=100`, '--paginate', '--slurp']);
      return { status: state.status, stdout: state.listing, stderr: state.stderr };
    }
    return spawnSync(program, args, options);
  };
  const verify = expected => verifyReleaseSource({ cwd, env, run, expected });
  return { directory, cwd, remote, env, git, state, run, verify };
}

test('existing lightweight tag binds the exact event commit in remote main history', t => {
  const f = fixture(t);
  const result = f.verify();
  assert.deepEqual(result, { schemaVersion: 1, repository, tag: 'v0.2.0', version: '0.2.0',
    sourceSha: f.env.GITHUB_SHA, tagObjectSha: f.env.GITHUB_SHA, trustedBranch: 'main' });
  assert.deepEqual(f.verify(result), result);
  assert.equal(f.git('status', '--porcelain'), '');
});

test('annotated tags preserve their object identity as well as the peeled source commit', t => {
  const f = fixture(t, { annotated: true });
  const binding = f.verify();
  assert.notEqual(binding.sourceSha, binding.tagObjectSha);
  assert.equal(binding.tagObjectSha, f.git('rev-parse', 'refs/tags/v0.2.0'));
});

test('manual dispatch may release only the tag at the selected event commit', t => {
  const f = fixture(t);
  Object.assign(f.env, { GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REF_TYPE: 'branch',
    GITHUB_REF_NAME: 'main', RELEASE_INPUT_TAG: 'v0.2.0', RELEASE_INPUT_VERSION: '0.2.0' });
  assert.equal(f.verify().sourceSha, f.env.GITHUB_SHA);
  fs.writeFileSync(path.join(f.cwd, 'tracked'), 'next'); f.git('commit', '-am', 'next');
  f.env.GITHUB_SHA = f.git('rev-parse', 'HEAD');
  assert.throws(() => f.verify(), /tag must point to the exact checked-out commit/);
});

test('version and tag validation rejects malformed/injected values without executing them', t => {
  const f = fixture(t);
  for (const version of ['01.2.0', '0.2', '0.2.0-rc.1', '0.2.0\n', '$(touch injected)', '0.3.0']) {
    f.env.RELEASE_INPUT_VERSION = version;
    assert.throws(() => f.verify(), /version/);
  }
  delete f.env.RELEASE_INPUT_VERSION;
  for (const tag of ['v0.3.0', '../../ref', 'v0.2.0\n', '$(touch injected)']) {
    f.env.GITHUB_REF_NAME = tag; assert.throws(() => f.verify(), /tag, event ref and version/);
  }
  assert.equal(fs.existsSync(path.join(f.cwd, 'injected')), false);
});

test('all contract versions, event type, event SHA and dispatch tag ref are required', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.cwd, 'package.json'), '{"version":"0.3.0"}');
  assert.throws(() => f.verify(), /match manifest, package and OpenAPI/);
  f.git('restore', 'package.json');
  f.env.GITHUB_EVENT_NAME = 'pull_request'; assert.throws(() => f.verify(), /tag push or explicit/);
  f.env.GITHUB_EVENT_NAME = 'push'; f.env.GITHUB_SHA = 'a'.repeat(40);
  assert.throws(() => f.verify(), /workflow event commit/);
  f.env.GITHUB_SHA = f.git('rev-parse', 'HEAD'); f.env.GITHUB_EVENT_NAME = 'workflow_dispatch';
  f.env.RELEASE_INPUT_TAG = 'v0.3.0'; assert.throws(() => f.verify(), /tag, event ref and version/);
});

test('tracked source edits or staged changes cannot be published as a tagged commit', t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.cwd, 'tracked'), 'dirty');
  assert.throws(() => f.verify(), /Tracked source must remain unchanged/);
  f.git('add', 'tracked'); assert.throws(() => f.verify(), /Tracked source must remain unchanged/);
});

test('tag at a commit outside remote main history is rejected', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.cwd, 'tracked'), 'unmerged'); f.git('commit', '-am', 'unmerged');
  f.git('tag', '-f', 'v0.2.0'); f.git('push', '--force', 'origin', 'refs/tags/v0.2.0');
  f.env.GITHUB_SHA = f.git('rev-parse', 'HEAD');
  assert.throws(() => f.verify(), /Source must belong to remote main history/);
});

test('missing remote main, missing tag and moved remote tag fail closed', t => {
  const f = fixture(t);
  f.git('--git-dir', f.remote, 'update-ref', '-d', 'refs/heads/main');
  assert.throws(() => f.verify(), /Trusted remote main branch is missing/);
  f.git('push', 'origin', 'main');
  f.git('--git-dir', f.remote, 'update-ref', '-d', 'refs/tags/v0.2.0');
  assert.throws(() => f.verify(), /Remote tag is missing/);
  fs.writeFileSync(path.join(f.cwd, 'tracked'), 'next'); f.git('commit', '-am', 'next');
  f.git('push', 'origin', 'HEAD:refs/heads/other');
  f.git('--git-dir', f.remote, 'update-ref', 'refs/tags/v0.2.0', f.git('rev-parse', 'HEAD'));
  f.git('checkout', '--detach', f.env.GITHUB_SHA);
  assert.throws(() => f.verify(), /Remote tag is missing or differs/);
});

test('annotated tag replacement on the same commit is rejected by saved binding', t => {
  const f = fixture(t, { annotated: true });
  const binding = f.verify();
  f.git('tag', '-f', '-a', 'v0.2.0', '-m', 'different annotation');
  assert.throws(() => f.verify(binding), /Remote tag is missing or differs/);
  f.git('push', '--force', 'origin', 'refs/tags/v0.2.0');
  assert.throws(() => f.verify(binding), /binding changed since build started/);
});

test('repository identity and expected binding cannot silently change', t => {
  const f = fixture(t);
  const binding = f.verify();
  f.state.origin = 'https://github.com/other/monitor.git';
  assert.throws(() => f.verify(), /Origin does not match/);
  f.state.origin = `git@github.com:${repository}.git`; assert.deepEqual(f.verify(), binding);
  f.env.GITHUB_SERVER_URL = 'https://other.invalid'; assert.throws(() => f.verify(), /requires github.com/);
  f.env.GITHUB_SERVER_URL = 'https://github.com';
  assert.throws(() => f.verify({ ...binding, sourceSha: 'a'.repeat(40) }), /binding changed/);
  assert.throws(() => f.verify({ ...binding, injected: true }), /binding changed/);
});

test('release listing rejects existing published releases or drafts on any page', t => {
  const f = fixture(t);
  for (const draft of [false, true]) {
    f.state.listing = JSON.stringify([[{ tag_name: 'v0.1.3' }], [{ tag_name: 'v0.2.0', draft }]]);
    assert.throws(() => f.verify(), /release or draft already exists/);
  }
  f.state.listing = JSON.stringify([[{ tag_name: 'v0.1.3', draft: false }], []]);
  assert.equal(f.verify().tag, 'v0.2.0');
});

test('CLI/auth/network errors and malformed release responses never mean absent release', t => {
  const f = fixture(t);
  f.state.status = 1; f.state.stderr = 'private-token-that-must-not-leak';
  assert.throws(() => f.verify(), error => error.message === 'List existing releases failed');
  f.state.status = 0;
  for (const response of ['not JSON', '{}', '[]', '[{}]', '[[null]]', '[[{}]]']) {
    f.state.listing = response; assert.throws(() => f.verify(), /Invalid.*release listing response/);
  }
});

test('CLI fails without partial JSON or secret stderr on invalid arguments and source', t => {
  const f = fixture(t);
  for (const args of [['--unknown'], ['--expected'], []]) {
    const result = spawnSync(process.execPath, [cli, ...args], { cwd: f.cwd, env: f.env, encoding: 'utf8' });
    assert.equal(result.status, 1); assert.equal(result.stdout, ''); assert(result.stderr);
  }
});

function cliFixture(t) {
  const f = fixture(t);
  const bin = path.join(f.directory, 'bin'); fs.mkdirSync(bin);
  const realGit = spawnSync('which', ['git'], { encoding: 'utf8' }).stdout.trim();
  assert(realGit);
  // Only the public origin URL is substituted. Ref resolution, ls-remote and
  // ancestry run actual Git against the fixture's local bare remote.
  fs.writeFileSync(path.join(bin, 'git'), `#!${process.execPath}\n` +
    `const args = process.argv.slice(2); if (args.join(' ') === 'remote get-url origin') { console.log(${JSON.stringify(`https://github.com/${repository}.git`)}); } else { const r = require('child_process').spawnSync(${JSON.stringify(realGit)}, args, { stdio: 'inherit', env: process.env }); process.exit(r.status ?? 1); }\n`, { mode: 0o755 });
  const log = path.join(f.directory, 'publisher.json');
  fs.writeFileSync(path.join(bin, 'gh'), `#!${process.execPath}\n` +
    `const args = process.argv.slice(2); if (args[0] === 'api') { require('assert').deepStrictEqual(args, ['api', '--hostname', 'github.com', '--method', 'GET', 'repos/${repository}/releases?per_page=100', '--paginate', '--slurp']); console.log('[[]]'); } else if (args[0] === 'release' && args[1] === 'create') { require('fs').writeFileSync(process.env.FIXTURE_COMMAND_LOG, JSON.stringify(args)); process.exit(Number(process.env.FIXTURE_GH_EXIT || 0)); } else { process.exit(99); }\n`, { mode: 0o755 });
  Object.assign(f.env, { PATH: `${bin}:${path.dirname(process.execPath)}:${process.env.PATH}`,
    FIXTURE_COMMAND_LOG: log });
  const runCli = args => spawnSync(process.execPath, [cli, ...args],
    { cwd: f.cwd, env: f.env, encoding: 'utf8', timeout: 10000 });
  return { ...f, log, runCli };
}

test('real CLI passes matching expected binding and refuses stale binding in valid Git state', t => {
  const f = cliFixture(t);
  const initial = f.runCli([]); assert.equal(initial.status, 0, initial.stderr);
  const binding = JSON.parse(initial.stdout);
  const expectedPath = path.join(f.directory, 'binding.json');
  fs.writeFileSync(expectedPath, JSON.stringify(binding));
  const matched = f.runCli(['--expected', expectedPath]);
  assert.equal(matched.status, 0, matched.stderr); assert.deepEqual(JSON.parse(matched.stdout), binding);
  fs.writeFileSync(expectedPath, JSON.stringify({ ...binding, tagObjectSha: 'b'.repeat(40) }));
  const stale = f.runCli(['--expected', expectedPath]);
  assert.equal(stale.status, 1); assert.equal(stale.stdout, '');
  assert.match(stale.stderr, /binding changed since build started/);
  assert.equal(fs.existsSync(f.log), false);
});

function publishFixture(t) {
  const f = cliFixture(t);
  for (const sub of ['scripts', 'release/out', 'runner']) fs.mkdirSync(path.join(f.cwd, sub), { recursive: true });
  for (const script of ['verify-release-source.mjs', 'verify-release-assets.mjs']) {
    fs.copyFileSync(new URL(`../scripts/${script}`, import.meta.url), path.join(f.cwd, 'scripts', script));
  }
  const initial = f.runCli([]); assert.equal(initial.status, 0, initial.stderr);
  const binding = JSON.parse(initial.stdout);
  const output = path.join(f.cwd, 'release/out');
  const provenance = path.join(output, 'source-provenance.json');
  fs.writeFileSync(provenance, JSON.stringify(binding));
  const payloadNames = ['source-provenance.json'];
  for (const suffix of ['macos-arm64.zip', 'macos-x64.zip', 'windows-x64.zip', 'linux-x64.tar.gz']) {
    const name = `sya-function-monitor-0.2.0-${suffix}`;
    for (const filename of [name, `${name}.digests.json`]) {
      fs.writeFileSync(path.join(output, filename), `synthetic ${filename}`); payloadNames.push(filename);
    }
  }
  const checksums = () => {
    const result = spawnSync('sha256sum', payloadNames, { cwd: output, env: f.env, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    fs.writeFileSync(path.join(output, 'SHA256SUMS.txt'), result.stdout);
  };
  checksums();
  const block = workflow.match(/      - name: Verify binding and create a new GitHub Release\n[\s\S]*?        run: \|\n([\s\S]+)$/)?.[1];
  assert(block, 'actual publish shell must exist');
  const script = block.replace(/^          /gm, '');
  assert(!script.includes('${{'), 'untrusted Actions expressions must use environment variables');
  Object.assign(f.env, { RUNNER_TEMP: path.join(f.cwd, 'runner'), RELEASE_TAG: 'v0.2.0' });
  const run = extra => spawnSync('bash', ['--noprofile', '--norc', '-c', script],
    { cwd: f.cwd, env: { ...f.env, ...extra }, encoding: 'utf8', timeout: 15000 });
  return { ...f, binding, output, provenance, payloadNames, checksums, run };
}

test('actual publish shell executes real guard and passes only the validated asset array', t => {
  const f = publishFixture(t);
  assert.equal(f.run({ FIXTURE_GH_EXIT: '1' }).status, 1, 'publisher rejection must stop the job');
  const args = JSON.parse(fs.readFileSync(f.log));
  assert.deepEqual(args.slice(0, 3), ['release', 'create', 'v0.2.0']);
  const expectedAssets = [...f.payloadNames, 'SHA256SUMS.txt'].sort().map(name => `release/out/${name}`);
  assert.deepEqual(args.slice(3, 3 + expectedAssets.length), expectedAssets);
  assert(args.includes('--verify-tag')); assert(args.includes('--latest=false'));
  assert(args.includes(`github.com/${repository}`));
  assert(!args.some(arg => ['--clobber', 'edit', 'upload', 'delete'].includes(arg)));
  const success = f.run(); assert.equal(success.status, 0, success.stderr);
});

test('actual publish shell refuses stale binding even when its checksum is valid', t => {
  const f = publishFixture(t);
  fs.writeFileSync(f.provenance, JSON.stringify({ ...f.binding, tagObjectSha: 'b'.repeat(40) }));
  f.checksums();
  const result = f.run(); assert.equal(result.status, 1);
  assert.match(result.stderr, /binding changed since build started/);
  assert.equal(fs.existsSync(f.log), false, 'stale expected binding must prevent publisher');
});

for (const name of ['unexpected.txt', '.unexpected']) {
  test(`actual publish shell rejects added asset ${name} after checksums`, t => {
    const f = publishFixture(t);
    fs.writeFileSync(path.join(f.output, name), 'unchecked');
    const result = f.run(); assert.equal(result.status, 1);
    assert.match(result.stderr, /exactly the expected asset set/);
    assert.equal(fs.existsSync(f.log), false);
  });
}

test('actual publish shell rejects missing and mutated expected assets', t => {
  const f = publishFixture(t);
  const target = path.join(f.output, f.payloadNames[1]);
  const original = fs.readFileSync(target);
  fs.unlinkSync(target);
  const missing = f.run(); assert.equal(missing.status, 1);
  assert.match(missing.stderr, /exactly the expected asset set/);
  assert.equal(fs.existsSync(f.log), false);
  fs.writeFileSync(target, Buffer.concat([original, Buffer.from('changed')]));
  const mutated = f.run(); assert.equal(mutated.status, 1);
  assert.match(mutated.stdout + mutated.stderr, /FAILED|did NOT match/);
  assert.equal(fs.existsSync(f.log), false);
});

test('actual publish shell requires checksum coverage exactly once and regular files', t => {
  const f = publishFixture(t);
  const checksumPath = path.join(f.output, 'SHA256SUMS.txt');
  const lines = fs.readFileSync(checksumPath, 'utf8').trimEnd().split('\n');
  for (const corrupted of [lines.slice(1), [lines[1], ...lines.slice(1)]]) {
    fs.writeFileSync(checksumPath, `${corrupted.join('\n')}\n`);
    const result = f.run(); assert.equal(result.status, 1);
    assert.match(result.stderr, /cover each expected payload exactly once/);
    assert.equal(fs.existsSync(f.log), false);
  }
  f.checksums();
  const target = path.join(f.output, f.payloadNames[1]);
  const external = path.join(f.directory, 'external'); fs.renameSync(target, external);
  fs.symlinkSync(external, target);
  const result = f.run(); assert.equal(result.status, 1);
  assert.match(result.stderr, /regular file/); assert.equal(fs.existsSync(f.log), false);
});
