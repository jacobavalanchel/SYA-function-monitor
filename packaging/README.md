# Monitor 原生候选

本目录只实现 macOS arm64、CPython 3.11 的本机候选，不接入发布 workflow，也不替换 SYA 的 artifact lock。当前构建机为 macOS 27；Homebrew Python 3.11.16 的实际最低系统为 26，libusb 为 15，MediaPipe 主扩展为 14.5。不能把 `MACOSX_DEPLOYMENT_TARGET=12.0` 或 wheel 文件名当作 macOS 12 兼容证据。

## 构建输入

1. 在新建任务目录创建 Python 3.11 venv；清理继承的索引、Python/dyld 覆盖、provider/release 凭据。按 `requirements-macos-arm64.lock` 使用 `pip download --require-hashes --only-binary :all: --dest /tmp/monitor-native-task/wheels -r packaging/requirements-macos-arm64.lock` 保存批准 wheel，再用 `pip install --no-index --find-links /tmp/monitor-native-task/wheels --require-hashes --only-binary :all: -r packaging/requirements-macos-arm64.lock` 安装完整依赖图。固定版本、解析来源和许可见 [dependency-notes.md](dependency-notes.md)。不要修改用户已有环境。
2. 用同一个 Python 执行 `scripts/realsense_l515.py install --root /tmp/monitor-native-task/sdk-build --venv /tmp/monitor-native-task/venv`，复用固定 librealsense 2.54.2、pybind11 和 patch；这一步编译并导入 SDK，不执行 doctor、设备枚举或采集。该安装器使用 pip 26.0.1，须保留安装日志与 `install.json`。
3. 从真实安装结果生成一个独立 SDK 来源 JSON：保留 `install.json` 的 `sdkVersion`、`sdkCommit`、`pybind11Commit`、`patchSha256`、`architecture`、`pythonVersion`；新增 `modules` 对象，必须恰有 `pyrealsense2` / `pyrsutils`，各含其真实解析文件的绝对 `path` 与 `sha256`；`libraries` 是同格式数组，至少包括实际 librealsense2 和 libusb regular files。解析 symlink 后绑定真实文件，不能自行填写不存在的摘要。核对源码 HEAD、pybind11 HEAD、patch 和编译输入后再使用。
4. 执行以下命令；输出必须是新的系统临时目录，或本模块 ignored `build/` / `dist/` 下的新目录。既有目录、源码输入目录及用户配置目录被拒绝。

```bash
/tmp/monitor-native-task/venv/bin/python scripts/build-native-runtime.py \
  --sdk-provenance /tmp/monitor-native-task/sdk-provenance.json \
  --wheelhouse /tmp/monitor-native-task/wheels \
  --node-executable /absolute/path/to/node \
  --output /tmp/monitor-native-task/candidate
```

构建器校验完整依赖闭包、批准 wheel hashes、安装文件字节和 SDK 文件摘要；pip 是明确的引导工具例外。它先创建允许清单内的源码/合同快照，使用 PyInstaller 6.22.3 onefile；输入或安装字节在构建期间改变会拒绝成功。输出 `stage/bin/monitor-server`、外置合同文件、独立构建日志及全部门禁通过后的 `native-build-provenance.json`，记录来源快照、阶段树摘要及构建前后输入指纹。Node 路径必须显式提供，验证使用 CI 对应 Node22.17.1。不联网、不发现签名身份、不更新 lock，不生成 release 来源证明；报告固定 `releaseEligible:false`。

## 可验证行为

冻结程序的 `--self-test` 在空 PATH、隔离 HOME/config/记录目录运行；重复验证中文和空格迁移路径。它真实运行 5 次 FaceMesh 合成无脸检测、Pillow JPEG 编码解码、固定 SDK 导入及模型资源摘要；通过 dyld 已载入库检查拒绝冻结目录外的非系统库。无脸测试不覆盖 478 landmark 分支，不证明真实注意力准确率或精力状态。它不创建 SDK context、不枚举设备、不读取 AX/屏幕/摄像头或请求模型。

构建器强制执行原始与迁移路径的 HTTP 验证，复用 `scripts/test-staged-runtime.mjs`，以绝对 Node 路径和无 Python PATH 执行新 stage，验证随机 token、进程 HMAC、全部声明操作的认证拒绝、安全 GET、SSE 及有界进程清理。内部推理时间与外部启动到退出总耗时分开记录。server 的 SIGTERM 清理只释放已持有资源，不调用会枚举设备的 `status()`，关闭失败非零退出，关闭开始后拒绝排队的新采集启动。

冻结入口显式使用已锁定 certifi 的包内公共 CA，关闭构建机 CA 目录回退，保留证书和主机名校验；离线自检确认根证书集合及摘要。源码入口保持原有行为。当前候选没有新增用户/企业 CA 配置。

## 当前依赖异常与发布边界

官方 MediaPipe 0.10.21 cp311 universal2 wheel 的内部 WHEEL 标签错误，`pip check` 在本机失败；三个多余嵌套扩展只有 x86_64。实际主扩展包含 arm64，合成 FaceMesh 可运行。完整证据和官方 issue 在 [依赖说明](dependency-notes.md)。没有修改安装元数据或把该失败改报成功；当前允许本机诊断，但尚未通过生产依赖验收。

后续须修复可信 wheel 输入并完成全依赖许可材料、最老支持系统、四平台、真实设备、签名/公证/安装验收。Electron 现有签名器可能重签 native sidecar，Apple code/data 布局及 lock 摘要一致性也须解决。onefile 原型或本地 hash 均不能证明可信 Release；不放宽现有消费者完整性门禁。

2026-10-13 的本机 `candidate-7` 已通过构建器全部门禁：原位与中文／空格迁移均完成FaceMesh合成无脸、JPEG、包内CA／OpenSSL及HTTP鉴权／SSE，142个已加载非系统native库均位于包内。两次运行的stage摘要相同，进程已清理；报告仍为 `releaseEligible:false`。结果只适用于本机macOS27 arm64候选，不证明真实设备、landmark准确率或正式Electron安装包。
