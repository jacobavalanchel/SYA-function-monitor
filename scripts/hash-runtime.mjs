import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// sya-runtime-tree-sha256-v1: copied verbatim from SYA-UI's
// scripts/hash-function-runtime.mjs at 6c3366eed46e4af5751b90ae0706fe45dc4586d7.
// Keep the function identical; tests check the fixed format and consumer parity.
export function runtimeTreeSha256(runtimeDir) {
  const resolvedRoot = path.resolve(runtimeDir);
  const hash = createHash('sha256');
  const updateField = (label, value) => {
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(value);
    hash.update(`${label}\0${bytes.byteLength}\0`);
    hash.update(bytes);
  };
  const visit = (directory) => {
    const entries = fs.readdirSync(directory);
    const canonicalEntries = new Set(entries.map((entry) => entry.normalize('NFC')));
    if (canonicalEntries.size !== entries.length) {
      throw new Error(`Runtime directory contains Unicode-normalization-equivalent entries: ${directory}`);
    }
    for (const entry of entries.sort((left, right) => {
      const canonicalOrder = Buffer.compare(
        Buffer.from(left.normalize('NFC'), 'utf8'),
        Buffer.from(right.normalize('NFC'), 'utf8')
      );
      return canonicalOrder || Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8'));
    })) {
      const absolutePath = path.join(directory, entry);
      const relativePath = path.relative(resolvedRoot, absolutePath).split(path.sep).join('/').normalize('NFC');
      const stat = fs.lstatSync(absolutePath);
      if (entry === 'artifact-provenance.json' && directory === resolvedRoot) {
        if (!stat.isFile() || stat.isSymbolicLink()) {
          throw new Error('artifact-provenance.json must be a regular file.');
        }
        continue;
      }
      if (stat.isDirectory()) {
        updateField('directory', relativePath);
        visit(absolutePath);
      } else if (stat.isSymbolicLink()) {
        throw new Error(`Runtime archives must not contain symbolic links: ${relativePath}`);
      } else if (stat.isFile()) {
        updateField('file', relativePath);
        updateField('content', fs.readFileSync(absolutePath));
      } else {
        throw new Error(`Unsupported runtime entry: ${relativePath}`);
      }
    }
  };
  visit(resolvedRoot);
  return hash.digest('hex');
}

export function archiveSha256(archivePath) {
  if (!fs.lstatSync(archivePath).isFile()) {
    throw new Error('Archive must be a regular file, not a symbolic link.');
  }
  const descriptor = fs.openSync(archivePath, 'r');
  try {
    if (!fs.fstatSync(descriptor).isFile()) throw new Error('Archive must be a regular file.');
    const hash = createHash('sha256');
    const buffer = Buffer.alloc(64 * 1024);
    let length;
    while ((length = fs.readSync(descriptor, buffer, 0, buffer.length, null)) > 0) {
      hash.update(buffer.subarray(0, length));
    }
    return hash.digest('hex');
  } finally {
    fs.closeSync(descriptor);
  }
}

export function publisherDigestMetadata(runtimeDir, { archivePath } = {}) {
  if (fs.lstatSync(path.join(runtimeDir, 'artifact-provenance.json'), { throwIfNoEntry: false })) {
    throw new Error('Producer stage must not contain consumer provenance.');
  }
  const metadata = {
    schemaVersion: 1,
    algorithm: 'sya-runtime-tree-sha256-v1',
    runtimeSha256: runtimeTreeSha256(runtimeDir),
  };
  if (archivePath !== undefined) metadata.sha256 = archiveSha256(archivePath);
  return metadata;
}

function main(args) {
  const usage = 'Usage: node scripts/hash-runtime.mjs <staged-directory> [--json] [--archive <archive-file>]';
  if (args.length === 1 && args[0] === '--help') {
    console.log(usage);
    return;
  }
  let runtimeDir;
  let archivePath;
  let json = false;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === '--json' && !json) {
      json = true;
    } else if (argument === '--archive' && archivePath === undefined) {
      archivePath = args[++index];
      if (!archivePath || archivePath.startsWith('--')) throw new Error(usage);
    } else if (!argument.startsWith('--') && runtimeDir === undefined) {
      runtimeDir = argument;
    } else {
      throw new Error(usage);
    }
  }
  if (!runtimeDir) throw new Error(usage);
  const metadata = publisherDigestMetadata(runtimeDir, { archivePath });
  console.log(json || archivePath !== undefined ? JSON.stringify(metadata) : metadata.runtimeSha256);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
