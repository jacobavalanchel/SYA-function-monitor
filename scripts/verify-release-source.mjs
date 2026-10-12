import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const normalVersion = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const sha = /^[a-f0-9]{40}$/;

function readJson(filename) {
  try { return JSON.parse(fs.readFileSync(filename, 'utf8')); }
  catch { throw new Error(`Cannot read release metadata: ${path.basename(filename)}`); }
}

// Read-only: Git reads source/ref state; gh only lists releases. Publishing stays
// in the workflow's create-only gh command, never in this guard or its tests.
export function verifyReleaseSource({ cwd = process.cwd(), env = process.env, expected,
  run = spawnSync } = {}) {
  const command = (program, args, label) => {
    const result = run(program, args, { cwd, env, encoding: 'utf8', timeout: 30000,
      maxBuffer: 16 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
    // Do not echo subprocess stderr: Git URLs or CLI diagnostics may contain credentials.
    if (result.error || result.status !== 0) throw new Error(`${label} failed`);
    return result.stdout.trim();
  };
  const git = (args, label) => command('git', args, label);
  const repository = env.GITHUB_REPOSITORY;
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(repository ?? '')) {
    throw new Error('GITHUB_REPOSITORY must identify the publishing repository');
  }
  if (env.GITHUB_SERVER_URL && env.GITHUB_SERVER_URL !== 'https://github.com') {
    throw new Error('This release workflow requires github.com');
  }
  const origin = git(['remote', 'get-url', 'origin'], 'Read origin');
  const allowedOrigins = [`https://github.com/${repository}`, `git@github.com:${repository}`,
    `ssh://git@github.com/${repository}`];
  if (!allowedOrigins.some(value => origin.replace(/\.git$/, '').toLowerCase() === value.toLowerCase())) {
    throw new Error('Origin does not match the publishing repository');
  }
  const manifestVersion = readJson(path.join(cwd, 'manifest.json')).version;
  const version = env.RELEASE_INPUT_VERSION || manifestVersion;
  if (typeof version !== 'string' || version.length > 64 || version !== version.trim() || !normalVersion.test(version)) {
    throw new Error('Release version must be a normal X.Y.Z version without leading zeroes');
  }
  if (version !== manifestVersion || version !== readJson(path.join(cwd, 'package.json')).version
    || version !== readJson(path.join(cwd, 'api.openapi.json')).info?.version) {
    throw new Error('Release version must match manifest, package and OpenAPI versions');
  }
  let tag;
  if (env.GITHUB_EVENT_NAME === 'push' && env.GITHUB_REF_TYPE === 'tag') {
    tag = env.GITHUB_REF_NAME;
  } else if (env.GITHUB_EVENT_NAME === 'workflow_dispatch') {
    tag = env.RELEASE_INPUT_TAG;
  } else {
    throw new Error('Release requires a tag push or explicit workflow dispatch');
  }
  if (tag !== `v${version}` || (env.GITHUB_REF_TYPE === 'tag' && env.GITHUB_REF_NAME !== tag)) {
    throw new Error('Release tag, event ref and version do not match');
  }
  const sourceSha = git(['rev-parse', '--verify', 'HEAD^{commit}'], 'Resolve source commit');
  if (!sha.test(sourceSha) || sourceSha !== env.GITHUB_SHA) {
    throw new Error('Checked-out HEAD must equal the workflow event commit');
  }
  git(['diff', '--quiet', 'HEAD', '--'], 'Tracked source must remain unchanged');
  const tagRef = `refs/tags/${tag}`;
  const tagObjectSha = git(['rev-parse', '--verify', tagRef], 'Resolve existing tag');
  const tagCommit = git(['rev-parse', '--verify', `${tagRef}^{commit}`], 'Resolve tag commit');
  if (!sha.test(tagObjectSha) || tagCommit !== sourceSha) {
    throw new Error('Existing tag must point to the exact checked-out commit');
  }
  const refs = new Map();
  const remoteRefs = git(['ls-remote', '--exit-code', 'origin', 'refs/heads/main', tagRef,
    `${tagRef}^{}`], 'Read authoritative remote refs');
  for (const line of remoteRefs.split('\n')) {
    const [value, ref, extra] = line.split(/\s+/);
    if (!sha.test(value ?? '') || !ref || extra || refs.has(ref)) {
      throw new Error('Invalid remote reference response');
    }
    refs.set(ref, value);
  }
  if (refs.get(tagRef) !== tagObjectSha || (refs.get(`${tagRef}^{}`) ?? refs.get(tagRef)) !== sourceSha) {
    throw new Error('Remote tag is missing or differs from the checked-out tag');
  }
  const mainSha = refs.get('refs/heads/main');
  if (!mainSha) throw new Error('Trusted remote main branch is missing');
  // A main advancement after checkout is allowed only when its commit is already
  // present locally and ancestry can be proven. Missing history fails closed.
  git(['merge-base', '--is-ancestor', sourceSha, mainSha], 'Source must belong to remote main history');
  const binding = { schemaVersion: 1, repository, tag, version, sourceSha, tagObjectSha,
    trustedBranch: 'main' };
  if (expected && (Object.keys(expected).length !== Object.keys(binding).length
    || Object.entries(binding).some(([key, value]) => expected[key] !== value))) {
    throw new Error('Release source binding changed since build started');
  }
  const releasesJson = command('gh', ['api', '--hostname', 'github.com', '--method', 'GET',
    `repos/${repository}/releases?per_page=100`, '--paginate', '--slurp'], 'List existing releases');
  let pages;
  try { pages = JSON.parse(releasesJson); } catch { throw new Error('Invalid release listing response'); }
  if (!Array.isArray(pages) || pages.length === 0 || pages.some(page => !Array.isArray(page)
    || page.some(release => !release || typeof release.tag_name !== 'string'))) {
    throw new Error('Invalid paginated release listing response');
  }
  // The contents:write CI token can list drafts as well as published releases.
  // Never reinterpret a failed request as an absent release.
  if (pages.some(page => page.some(release => release.tag_name === tag))) {
    throw new Error('A release or draft already exists for this tag; refusing to replace it');
  }
  return binding;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 0 && (args.length !== 2 || args[0] !== '--expected' || !args[1])) {
      throw new Error('Usage: verify-release-source.mjs [--expected binding.json]');
    }
    const expected = args.length ? readJson(args[1]) : undefined;
    if (args.length && (!expected || Array.isArray(expected) || typeof expected !== 'object')) {
      throw new Error('Invalid expected source binding');
    }
    console.log(JSON.stringify(verifyReleaseSource({ expected }), null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
