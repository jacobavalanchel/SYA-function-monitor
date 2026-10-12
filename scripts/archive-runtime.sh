#!/usr/bin/env bash
# PR 与发布共用已通过 smoke 的目录与归档对照流程；此脚本不发布。
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Usage: archive-runtime.sh X.Y.Z [source-provenance.json]" >&2
  exit 2
fi
version="$1"
hash_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hash-runtime.mjs"
runtime_sha256="$(node -p "JSON.parse(require('fs').readFileSync('release/smoke-result.json', 'utf8')).runtimeSha256")"
[[ "${runtime_sha256}" =~ ^[a-f0-9]{64}$ ]]
test "${runtime_sha256}" = "$(node "${hash_script}" release/stage)"
mkdir release/out
platforms=(
  "macos-arm64:zip"
  "macos-x64:zip"
  "windows-x64:zip"
  "linux-x64:tar.gz"
)
for item in "${platforms[@]}"; do
  platform="${item%%:*}"
  ext="${item#*:}"
  name="sya-function-monitor-${version}-${platform}.${ext}"
  if [[ "${ext}" == "zip" ]]; then
    (cd release/stage && zip -qr "../out/${name}" .)
  else
    tar -czf "release/out/${name}" -C release/stage .
  fi
  verify_dir="release/verify/${platform}"
  mkdir -p "${verify_dir}"
  if [[ "${ext}" == "zip" ]]; then
    unzip -q "release/out/${name}" -d "${verify_dir}"
  else
    tar -xzf "release/out/${name}" -C "${verify_dir}"
  fi
  test "${runtime_sha256}" = "$(node "${hash_script}" "${verify_dir}")"
  node "${hash_script}" release/stage --archive "release/out/${name}" > "release/out/${name}.digests.json"
done
test "${runtime_sha256}" = "$(node "${hash_script}" release/stage)"
if [[ $# -eq 2 ]]; then
  cp "$2" release/out/source-provenance.json
fi
(cd release/out && sha256sum * > SHA256SUMS.txt)
