import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { archiveSha256, publisherDigestMetadata, runtimeTreeSha256 } from '../scripts/hash-runtime.mjs';

const knownDigest = 'e15bf5abffd2ce6846eff66a6b094788486908c9359099a350edc5b146a90ad1';
const cli = fileURLToPath(new URL('../scripts/hash-runtime.mjs', import.meta.url));
const parentConsumer = new URL('../../../scripts/hash-function-runtime.mjs', import.meta.url);

function temporaryDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-release-digest-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function fixture(t, unicodeName = '\u00e9.bin') {
  const directory = temporaryDirectory(t);
  fs.mkdirSync(path.join(directory, 'bin'));
  fs.mkdirSync(path.join(directory, 'empty'));
  fs.writeFileSync(path.join(directory, 'A.txt'), 'A\n');
  fs.writeFileSync(path.join(directory, 'bin', unicodeName), Buffer.from([0, 1, 255]));
  fs.writeFileSync(path.join(directory, 'z.txt'), 'Z');
  return directory;
}

function runCli(args) {
  return spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8' });
}

test('canonical fixed vector includes binary content, UTF-8 byte lengths and empty directories', (t) => {
  // Independently framed as label + NUL + decimal byte length + NUL + bytes:
  // file A.txt; content A\n; directory bin; file bin/é.bin; content 00 01 ff;
  // directory empty; file z.txt; content Z. The expected digest is fixed, not
  // calculated with the implementation under test.
  assert.equal(runtimeTreeSha256(fixture(t)), knownDigest);
  assert.equal(runtimeTreeSha256(temporaryDirectory(t)),
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
});

test('NFC and NFD names have the same canonical digest', (t) => {
  assert.equal(runtimeTreeSha256(fixture(t, '\u00e9.bin')), knownDigest);
  assert.equal(runtimeTreeSha256(fixture(t, 'e\u0301.bin')), knownDigest);
});

test('entry sorting follows UTF-8 bytes, including names whose UTF-16 order differs', (t) => {
  const directory = temporaryDirectory(t);
  for (const name of ['\u{10000}', 'a', '\ue000', 'Z']) fs.writeFileSync(path.join(directory, name), '');
  const expected = createHash('sha256');
  for (const name of ['Z', 'a', '\ue000', '\u{10000}']) {
    expected.update(`file\0${Buffer.byteLength(name)}\0${name}content\x000\0`);
  }
  assert.equal(runtimeTreeSha256(directory), expected.digest('hex'));
});

test('only a regular provenance file at the root is excluded', (t) => {
  const directory = fixture(t);
  const provenance = path.join(directory, 'artifact-provenance.json');
  fs.writeFileSync(provenance, '{"local":"untrusted"}');
  assert.equal(runtimeTreeSha256(directory), knownDigest);
  fs.writeFileSync(provenance, 'changed metadata');
  assert.equal(runtimeTreeSha256(directory), knownDigest);
  fs.writeFileSync(path.join(directory, 'bin', 'artifact-provenance.json'), 'nested');
  assert.notEqual(runtimeTreeSha256(directory), knownDigest);
  fs.unlinkSync(provenance);
  fs.mkdirSync(provenance);
  assert.throws(() => runtimeTreeSha256(directory), /artifact-provenance.json must be a regular file/);
});

test('content and empty directory changes alter the digest', (t) => {
  const directory = fixture(t);
  fs.writeFileSync(path.join(directory, 'z.txt'), 'different');
  assert.notEqual(runtimeTreeSha256(directory), knownDigest);
  fs.writeFileSync(path.join(directory, 'z.txt'), 'Z');
  fs.rmdirSync(path.join(directory, 'empty'));
  assert.notEqual(runtimeTreeSha256(directory), knownDigest);
});

test('publisher entry and CLI refuse root consumer provenance without output', (t) => {
  const directory = fixture(t);
  const provenance = path.join(directory, 'artifact-provenance.json');
  fs.writeFileSync(provenance, '{}');
  assert.throws(() => publisherDigestMetadata(directory), /must not contain consumer provenance/);
  for (const args of [[directory], [directory, '--json'], [directory, '--archive', 'unused.zip']]) {
    const result = runCli(args);
    assert.equal(result.status, 1);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /must not contain consumer provenance/);
  }
  fs.unlinkSync(provenance);
  fs.mkdirSync(provenance);
  assert.throws(() => publisherDigestMetadata(directory), /must not contain consumer provenance/);
});

test('canonical-equivalent sibling names are rejected before reading their content', (t) => {
  const directory = temporaryDirectory(t);
  // macOS filesystems may already merge these names, so model the directory
  // listing explicitly to exercise this rejection on every filesystem.
  t.mock.method(fs, 'readdirSync', () => ['\u00e9', 'e\u0301']);
  assert.throws(() => runtimeTreeSha256(directory), /Unicode-normalization-equivalent entries/);
});

test('symbolic links, including root provenance, are rejected', (t) => {
  const directory = fixture(t);
  const link = path.join(directory, 'link');
  try {
    fs.symlinkSync(path.join(directory, 'A.txt'), link);
  } catch (error) {
    if (error.code === 'EPERM' || error.code === 'ENOTSUP') return t.skip('Symbolic links unavailable');
    throw error;
  }
  assert.throws(() => runtimeTreeSha256(directory), /must not contain symbolic links/);
  fs.renameSync(link, path.join(directory, 'artifact-provenance.json'));
  assert.throws(() => runtimeTreeSha256(directory), /artifact-provenance.json must be a regular file/);
  fs.unlinkSync(path.join(directory, 'A.txt'));
  assert.throws(() => publisherDigestMetadata(directory), /must not contain consumer provenance/,
    'A dangling root provenance link still counts as present');
});

test('unsupported filesystem entries are rejected', (t) => {
  const directory = temporaryDirectory(t);
  fs.writeFileSync(path.join(directory, 'unsupported'), '');
  t.mock.method(fs, 'lstatSync', () => ({ isDirectory: () => false, isSymbolicLink: () => false, isFile: () => false }));
  assert.throws(() => runtimeTreeSha256(directory), /Unsupported runtime entry: unsupported/);
});

test('archive hashing uses archive bytes and returns publisher digest fields only', (t) => {
  const directory = fixture(t);
  const archive = path.join(temporaryDirectory(t), 'runtime.zip');
  fs.writeFileSync(archive, 'abc');
  assert.equal(archiveSha256(archive), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  const metadata = publisherDigestMetadata(directory, { archivePath: archive });
  assert.deepEqual(metadata, {
    schemaVersion: 1,
    algorithm: 'sya-runtime-tree-sha256-v1',
    runtimeSha256: knownDigest,
    sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
  });
  const largerArchive = Buffer.alloc(64 * 1024 * 2 + 17, 0xab);
  fs.writeFileSync(archive, largerArchive);
  assert.equal(archiveSha256(archive), createHash('sha256').update(largerArchive).digest('hex'));
  assert.throws(() => archiveSha256(directory), /Archive must be a regular file/);
});

test('archive symlinks are rejected', (t) => {
  const directory = temporaryDirectory(t);
  const archive = path.join(directory, 'archive');
  fs.writeFileSync(archive, 'abc');
  const link = path.join(directory, 'link');
  try {
    fs.symlinkSync(archive, link);
  } catch (error) {
    if (error.code === 'EPERM' || error.code === 'ENOTSUP') return t.skip('Symbolic links unavailable');
    throw error;
  }
  assert.throws(() => archiveSha256(link), /Archive must be a regular file/);
});

test('CLI emits plain digest or JSON, and emits no partial metadata on errors', (t) => {
  const directory = fixture(t);
  const archive = path.join(temporaryDirectory(t), 'runtime.zip');
  fs.writeFileSync(archive, 'abc');
  const plain = runCli([directory]);
  assert.equal(plain.status, 0, plain.stderr);
  assert.equal(plain.stdout, `${knownDigest}\n`);
  assert.equal(plain.stderr, '');
  for (const args of [[directory, '--json'], ['--json', directory]]) {
    const result = runCli(args);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), publisherDigestMetadata(directory));
  }
  const withArchive = runCli([directory, '--archive', archive]);
  assert.equal(withArchive.status, 0, withArchive.stderr);
  assert.deepEqual(JSON.parse(withArchive.stdout), publisherDigestMetadata(directory, { archivePath: archive }));
  for (const args of [[], [directory, '--unknown'], [directory, '--json', '--json'],
    [directory, '--archive'], [directory, '--archive', '--json'], [directory, directory],
    [directory, '--archive', `${archive}.missing`], [`${directory}.missing`]]) {
    const result = runCli(args);
    assert.equal(result.status, 1, JSON.stringify(args));
    assert.equal(result.stdout, '', JSON.stringify(args));
    assert.notEqual(result.stderr, '');
  }
});

test('producer implementation and digests match the parent consumer when available', async (t) => {
  if (!fs.existsSync(parentConsumer)) return t.skip('Standalone Monitor checkout has no parent consumer');
  const consumer = await import(parentConsumer.href);
  assert.equal(runtimeTreeSha256.toString(), consumer.runtimeTreeSha256.toString(),
    'Canonical function drifted; reconcile with the consumer before publishing');
  for (const directory of [fixture(t), fixture(t, 'e\u0301.bin'), temporaryDirectory(t)]) {
    assert.equal(runtimeTreeSha256(directory), consumer.runtimeTreeSha256(directory));
  }
});
