#!/usr/bin/env bash
# CI 与发布使用同一暂存配方；当前 wrapper 仍依赖外部 Python。
set -euo pipefail
if [[ $# -gt 1 ]]; then
  echo "Usage: bash scripts/stage-runtime.sh [new-output-directory]" >&2
  exit 2
fi
module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${module_root}"
stage_root="${1:-release/stage}"
if [[ "${stage_root}" != /* ]]; then
  stage_root="${module_root}/${stage_root}"
fi
if [[ -e "${stage_root}" || -L "${stage_root}" ]]; then
  echo "Staging destination already exists; choose a new directory." >&2
  exit 1
fi
# 解析尚不存在的输出路径，也解析其已存在父目录中的符号链接。
stage_root="$(node -e '
  const fs = require("fs"), path = require("path");
  let parent = path.resolve(process.argv[1]); const tail = [];
  while (!fs.existsSync(parent)) { tail.unshift(path.basename(parent)); parent = path.dirname(parent); }
  process.stdout.write(path.join(fs.realpathSync(parent), ...tail));
' "${stage_root}")"
case "${stage_root}/" in
  "${module_root}/scripts/"*|"${module_root}/patches/"*|"${module_root}/dist/"*)
    echo "Staging destination must be outside copied input directories." >&2
    exit 1
    ;;
esac
mkdir -p "$(dirname "${stage_root}")"
mkdir "${stage_root}"
mkdir "${stage_root}/bin" "${stage_root}/assets" "${stage_root}/dist"
cp manifest.json api.openapi.json README.md package.json module.json "${stage_root}/"
# package.json 的三个编译入口；不把构建目录中的无关文件混入发布包。
for entry in main renderer types; do
  cp "dist/${entry}.js" "dist/${entry}.d.ts" "${stage_root}/dist/"
done
cp index.html server.py realsense_runtime.py config.example.json "${stage_root}/assets/"
cp requirements-realsense.txt "${stage_root}/"
# 只复制版本控制中的源码，忽略 untracked/ignored helper 或凭据文件。
git ls-files -z -- scripts patches | while IFS= read -r -d '' source_file; do
  test -f "${source_file}" && test ! -L "${source_file}"
  mkdir -p "${stage_root}/$(dirname "${source_file}")"
  cp "${source_file}" "${stage_root}/${source_file}"
done
cat > "${stage_root}/bin/monitor-server" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
module_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_candidates=(
  "${MONITOR_PYTHON:-}"
  "$HOME/.sya/monitor/.venv/bin/python3"
  "python3"
)
for python_bin in "${python_candidates[@]}"; do
  if [[ -n "${python_bin}" ]] && command -v "${python_bin}" >/dev/null 2>&1; then
    exec "${python_bin}" "${module_root}/assets/server.py" "$@"
  fi
done
echo "No usable Python interpreter found for monitor runtime." >&2
exit 127
SCRIPT
cat > "${stage_root}/bin/monitor-server.cmd" <<'SCRIPT'
@echo off
set MODULE_ROOT=%~dp0..
if defined MONITOR_PYTHON (
  "%MONITOR_PYTHON%" "%MODULE_ROOT%\assets\server.py" %*
) else if exist "%USERPROFILE%\.sya\monitor\.venv\Scripts\python.exe" (
  "%USERPROFILE%\.sya\monitor\.venv\Scripts\python.exe" "%MODULE_ROOT%\assets\server.py" %*
) else (
  python "%MODULE_ROOT%\assets\server.py" %*
)
SCRIPT
chmod +x "${stage_root}/bin/monitor-server"
test -f "${stage_root}/manifest.json"
test -f "${stage_root}/api.openapi.json"
test -x "${stage_root}/bin/monitor-server"
test -f "${stage_root}/bin/monitor-server.cmd"
test -d "${stage_root}/assets"
test -f "${stage_root}/assets/realsense_runtime.py"
test -x "${stage_root}/scripts/realsense-l515"
test -f "${stage_root}/patches/librealsense-v2.54.2-macos-l515.patch"
