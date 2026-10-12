import fs from 'node:fs';
import path from 'node:path';

// Return only the reviewed release asset names, never a publish-time wildcard.
try {
  const [directory, version, extra] = process.argv.slice(2);
  if (!directory || extra || !version || version !== version.trim()
    || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(version)) {
    throw new Error('Usage: verify-release-assets.mjs output-directory X.Y.Z');
  }
  const payloadNames = ['source-provenance.json'];
  for (const suffix of ['macos-arm64.zip', 'macos-x64.zip', 'windows-x64.zip', 'linux-x64.tar.gz']) {
    const name = `sya-function-monitor-${version}-${suffix}`;
    payloadNames.push(name, `${name}.digests.json`);
  }
  const expectedNames = [...payloadNames, 'SHA256SUMS.txt'].sort();
  const actualNames = fs.readdirSync(directory).sort();
  if (actualNames.length !== expectedNames.length || actualNames.some((name, index) => name !== expectedNames[index])) {
    throw new Error('Release output does not contain exactly the expected asset set');
  }
  for (const name of expectedNames) {
    if (!fs.lstatSync(path.join(directory, name)).isFile()) {
      throw new Error('Every release asset must be a regular file');
    }
  }
  const lines = fs.readFileSync(path.join(directory, 'SHA256SUMS.txt'), 'utf8').trimEnd().split('\n');
  const checksumNames = lines.map(line => {
    const match = /^([a-f0-9]{64})  ([^\r\n]+)$/.exec(line);
    if (!match) throw new Error('Invalid release checksum entry');
    return match[2];
  }).sort();
  payloadNames.sort();
  if (checksumNames.length !== payloadNames.length || checksumNames.some((name, index) => name !== payloadNames[index])) {
    throw new Error('Release checksums must cover each expected payload exactly once');
  }
  // No output until the complete set and checksum coverage have been verified.
  console.log(expectedNames.map(name => path.join(directory, name)).join('\n'));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
