# macOS arm64 原生候选：依赖输入与证据

日期：2026-10-13。范围：CPython 3.11 的 Monitor 原生候选依赖解析；本文件不证明已经生成、签名或发布原生包，也不证明最低系统版本或真实设备可用。

## 输入与完整锁

- `requirements-macos-arm64.in` 固定 PyInstaller 6.22.3、NumPy 1.26.4、Pillow 11.3.0、MediaPipe 0.10.21、certifi 2026.7.22。
- `requirements-macos-arm64.lock` 使用标准 requirements 语法，锁定完整 **32 个 distribution**，包含 **828 个上游 distribution SHA-256**。未使用 `--no-deps`、依赖排除或 override；包括 MediaPipe 的 JAX/JAXLIB、OpenCV、Matplotlib、protobuf、sounddevice 等传递依赖。CA 修复新增 certifi；其余 31 个版本及全部原有 hash 集合保持不变。
- 解析器为 uv 0.12.10；目标为 CPython 3.11、`aarch64-apple-darwin`，显式 `MACOSX_DEPLOYMENT_TARGET=12.0`。uv 对该 target 的默认最低版本为 13.0，因此不能省略此输入。12.0 仅用于 wheel 标签筛选，最终支持范围仍取决于所选解释器、每个 native binary 和实际最低系统测试。[uv 参数说明](https://docs.astral.sh/uv/reference/cli/#uv-pip-compile)
- 仅使用 PyPI、稳定版本及 `2026-10-13T00:00:00Z` 前上传的文件。`--only-binary :all:` 禁止解析时执行 sdist 构建；其与本版本 uv 的 `--no-build` 不能同时传入。
- uv 的 `--generate-hashes` 保留所选版本的多个平台 distribution hashes，而非只保留最终安装的某一个 macOS wheel。安装也必须设置 `--only-binary :all:` 与 `--require-hashes`，并记录实际安装 wheel、版本、架构及其摘要。锁中出现其他平台或 sdist 的 hash 不表示它们获准用于本候选。[哈希安装规则](https://pip.pypa.io/en/stable/topics/secure-installs/)

在 Monitor 根目录重新解析：

```bash
MACOSX_DEPLOYMENT_TARGET=12.0 uv --no-config pip compile \
  packaging/requirements-macos-arm64.in \
  --python-version 3.11 \
  --python-platform aarch64-apple-darwin \
  --generate-hashes --only-binary :all: --no-python-downloads \
  --default-index https://pypi.org/simple --keyring-provider disabled \
  --prerelease disallow --exclude-newer 2026-10-13T00:00:00Z \
  --format requirements.txt \
  --output-file packaging/requirements-macos-arm64.lock
```

构建任务还应传入已审核的 `--python /absolute/path/to/python3.11`，清理继承的索引、配置和凭据环境。首次生成时采用 `env -i`、显式 PATH、上述 deployment target、`--no-cache` 和 Homebrew 3.11.16 的显式路径；只下载解析所需元数据，不创建或安装 venv。已有锁会被 uv 作为版本偏好使用；有意升级必须审阅新图与新 hashes。[uv 锁定与更新规则](https://docs.astral.sh/uv/pip/compile/)

安装应指向新建、任务专属的 CPython 3.11 环境，显式 `--python`、`--require-hashes --only-binary :all:`；随后检查完整依赖图并执行真实导入、合成 FaceMesh/JPEG 测试。不要对用户现有 venv 使用 sync，不要根据 health OK 推断推理依赖齐全。SDK 在完成 Python 依赖同步后单独安装，避免后续精确同步删除 SDK 模块。

## SDK 是另一个构建输入

官方 `pyrealsense2==2.54.2.5684` 没有 macOS wheel，因此它及 `pyrsutils` 不在本 PyPI 锁内；没有用第三方 macOS wheel 或较新 SDK 替代。[官方发布文件](https://pypi.org/project/pyrealsense2/2.54.2.5684/)

macOS 构建继续使用 `scripts/realsense_l515.py` 的已有固定输入：

| 输入 | 固定值 |
| --- | --- |
| librealsense | 2.54.2，commit `2dbaaf5964490cb02f811cf2ed38c8a893f5c027` |
| pybind11 | commit `0bd8896a4010f2d91b2340570c24fa08606ec406` |
| repository patch | `patches/librealsense-v2.54.2-macos-l515.patch` |
| patch SHA-256 | `eb3acdba968cb716133a176a0051af7d06568594cd3b1d8a65f6d3b1202317c1` |

隔离构建须另外记录 Python/编译器/CMake/SDK/libusb 的版本和构建参数，并在实际产生 `pyrealsense2`、`pyrsutils`、librealsense/libusb 后计算 binary hashes。当前没有在本锁里填入尚不存在的 SDK 产物摘要。构建完成后检查所有非系统依赖的 install-name/rpath，重定位后再签名和计算最终目录摘要。

## 本机解释器与最低系统边界

本轮只读 `uv python list --only-installed` 显示托管 Python 只有 3.12.13 和 3.13.15，没有可复用的托管 3.11。已安装的显式 3.11 路径为 `/opt/homebrew/opt/python@3.11/bin/python3.11`，版本 3.11.16，架构 arm64；适合本机隔离候选，但不适合据此宣称支持较老系统。

`otool` 的实际证据：该 Homebrew Python 的最低系统为 macOS **26.0**，其 Python framework 使用绝对 Homebrew install-name；现有 Homebrew libusb 1.0.30 的最低系统为 macOS **15.0**。设置 deployment target 不会降低这些已编译二进制的要求。产品若宣称更老的 macOS 支持，必须使用合格的解释器和依赖构建，并在最老支持系统实际测试。[PyInstaller macOS 构建要求](https://pyinstaller.org/en/stable/usage.html)

## 上游许可与资源来源

| 依赖 | 上游许可信息与来源 |
| --- | --- |
| PyInstaller 6.22.3 | GPLv2+ 与商业 bundle 例外；个别文件 Apache-2.0。例外不替代依赖各自的许可要求。[官方说明](https://pyinstaller.org/en/stable/license.html) |
| MediaPipe 0.10.21 | Apache-2.0；继续使用本版本 `solutions.face_mesh` 所需 wheel 内 graph/model，不能以较新 API 或缺失模型替代。[PyPI](https://pypi.org/project/mediapipe/0.10.21/) |
| NumPy 1.26.4 | BSD；wheel 内第三方组件还须保留各自 notices。[PyPI](https://pypi.org/project/numpy/1.26.4/) |
| Pillow 11.3.0 | MIT-CMU；随 wheel 的 native 图像库须一并清点许可。[PyPI](https://pypi.org/project/pillow/11.3.0/) |
| certifi 2026.7.22 | MPL-2.0；未改动的 Mozilla CA 集合及 wheel 内 `certifi-2026.7.22.dist-info/licenses/LICENSE` 随包保留。[固定版本 PyPI](https://pypi.org/project/certifi/2026.7.22/)、[官方来源与信任库规则](https://github.com/certifi/python-certifi) |
| librealsense 2.54.2 | Apache-2.0。[固定版本源码许可](https://github.com/realsenseai/librealsense/blob/v2.54.2/LICENSE) |
| libusb | LGPL-2.1；属于另行构建/搬运的 native 输入，须保留许可、对应来源及适用的替换/重链接材料。[官方站点](https://libusb.info/) |

完整锁中的传递 distribution 同样需要归档其实际安装版本的 LICENSE/NOTICE；不能只附上述顶层表。尤其 `pyinstaller-hooks-contrib`、OpenCV/JAX 等的打包资源可能带额外 notices。PyPI 元数据没有 license 字段时不能推断没有许可证，例如 `opt-einsum` 的许可证可在其[固定版本源码](https://github.com/dgasmith/opt_einsum/blob/v3.4.0/LICENSE)核查。本文件记录来源与待交付材料，不把元数据核对当作完整分发许可或安全审计。

## 已完成的锁验收

2026-10-13：完整解析成功；逐项请求官方 PyPI 版本 JSON，确认 31 个锁定版本和全部 826 个 hash 均存在于上游发布文件；再次以锁为输入执行完整解析，版本集合与每项 hashes 完全一致。锁定图包含 `jax/jaxlib 0.7.1`、`opencv-contrib-python 4.10.0.84`、`protobuf 4.25.9`、`pyinstaller-hooks-contrib 2026.8`。

锁生成阶段没有安装软件、启动摄像头/RealSense、读取屏幕/Accessibility、读取私人配置或凭据。依赖锁解析是构建输入证据；后续隔离安装发现的上游分发问题和 native imports 证据如下。冻结后模型资源、迁移路径启动、签名、公证、硬件与最低系统测试仍由后续构建验收给出。

## 隔离安装发现：MediaPipe 0.10.21 分发标签不一致

2026-10-13，按锁完成隔离安装后，`pip 26.2.1 check` 以非零状态返回 `mediapipe 0.10.21 is not supported on this platform`。已重新下载官方 `mediapipe-0.10.21-cp311-cp311-macosx_11_0_universal2.whl`，校验其 SHA-256 为 `b4946abad54d62812ce4c543187373a4872985fd66cd43fe7437f4b95d53d9ec`，并逐字节比较安装目录中的 `WHEEL` 和全部六个 Mach-O，均与官方归档一致。不是安装过程误选 x86_64 文件，也不是本地文件被改写。

该 wheel 内部 `WHEEL` 的唯一标签实际是 `cp311-cp311-macosx_14_0_x86_64`，与外部文件名的 `macosx_11_0_universal2` 不一致。官方 [MediaPipe issue #6112](https://github.com/google-ai-edge/mediapipe/issues/6112) 记录了同版本的这个问题；其关闭时讨论的是后来新发布的 arm64 wheels，不表示当前 0.10.21 文件已经修复。

扫描 wheel 内全部 Mach-O 得到以下证据，路径相对 `mediapipe/`：

| 路径 | 实际架构 | Mach-O 最低 macOS |
| --- | --- | --- |
| `python/_framework_bindings.cpython-311-darwin.so` | x86_64 + arm64 | 两个 slice 均 14.5 |
| `tasks/cc/metadata/python/_pywrap_metadata_version.cpython-311-darwin.so` | x86_64 + arm64 | 两个 slice 均 14.5 |
| `tasks/python/metadata/flatbuffers_lib/_pywrap_flatbuffers.cpython-311-darwin.so` | x86_64 + arm64 | 两个 slice 均 14.5 |
| `python/_framework_bindings/arm64.cpython-311-darwin.so` | 仅 x86_64 | 14.6 |
| `tasks/cc/metadata/python/_pywrap_metadata_version/arm64.cpython-311-darwin.so` | 仅 x86_64 | 14.6 |
| `tasks/python/metadata/flatbuffers_lib/_pywrap_flatbuffers/arm64.cpython-311-darwin.so` | 仅 x86_64 | 14.6 |

后三个文件各 4,152 字节、SHA-256 均为 `160d4110d93c2fe213449b9e02ecc776a2331fd2683a7cc8e0e1ef1f5a16b82d`，还带上游构建机的绝对 rpath。名称中的 `arm64` 不构成架构证据。前三个实际主扩展有 arm64 slice，因此这次错误不能概括为“整个 wheel 没有 arm64”；同时也不能将 wheel 当作全部内容均可用于 arm64 的干净分发。

用同一隔离 venv 的原生 arm64 CPython 3.11.16 运行了三个主扩展的真实 import，并通过各模块 `__file__` 确认加载的是前三个主扩展。随后执行 `FaceMesh(static_image_mode=True, max_num_faces=1, refine_landmarks=True)`，处理内存构造的 64×64×3 全零 RGB；图初始化和处理完成，返回零个人脸，进程退出码为 0。测试使用临时 Matplotlib 缓存与临时目录、禁用 bytecode 写入，未启动摄像头或读取真实图像。这证明当前系统下对应 native imports 和合成图像执行可用，不证明脸部识别准确性、冻结包完整性或 macOS 11/12 兼容。

后续处理保留以下边界：

1. 保留原 wheel/`WHEEL`/`RECORD`，保留 `pip check` 失败结果；不得修改标签、屏蔽 stderr 或把失败改报为依赖验收通过。当前本机原型测试只能与这项已知失败并列记录。
2. 正式分发需采用经过独立核验的新上游修正产物，或者从固定 MediaPipe 0.10.21 源码及固定工具链重新构建原生 wheel。重新构建必须检查实际架构、部署下限、资源和依赖，生成正确 `WHEEL`/`RECORD` 与新的产物摘要，再重跑干净安装、`pip check` 和模型测试；这不是重命名或改写现有 wheel 的捷径。[固定版本构建源](https://github.com/google-ai-edge/mediapipe/blob/v0.10.21/setup.py)、[官方 wheel 构建说明](https://developers.google.com/edge/mediapipe/solutions/build_python)
3. 若改用较新 MediaPipe，先核实 `solutions.face_mesh` 与模型/结果语义的兼容性；不为消除平台标签错误而隐式换 API 或丢弃推理能力。
4. 冻结时继续执行严格 native 架构与动态库检查。若收集到了上述嵌套 x86_64 文件，应定位实际导入/引用关系并记录处理证据；不能用全局排除 native 库或关闭 PyInstaller 架构检查让构建通过。

## 冻结包的公开 CA 信任库

原型评审发现 Homebrew OpenSSL 的默认 CA 路径仍指向 `/opt/homebrew`；只打包 SSL 动态库不能保证换机器后的 HTTPS 验证可用。新增 `native_tls.py`，仅冻结入口在导入服务或运行 self-test 前，核对包内 certifi 版本、路径及 CA 文件摘要，然后设置 `SSL_CERT_FILE` 为包内 CA，`SSL_CERT_DIR` 为空以阻止编译时目录回退。保留 `CERT_REQUIRED` 与主机名验证；不读取 Keychain、用户 CA 配置或私人证书，不关闭 TLS 验证。源码入口保持原有行为。自定义企业 CA 的导入或合并不在本候选范围内。[Python SSL 默认上下文与路径](https://docs.python.org/3.11/library/ssl.html#ssl.create_default_context)

按官方 PyPI JSON 筛选，2026-10-13 截止日前最新稳定版为 **certifi 2026.7.22**，于 2026-07-22 发布。先校验官方 wheel，再读取其中未修改的 CA 文件得到：

| 内容 | SHA-256 |
| --- | --- |
| `certifi-2026.7.22-py3-none-any.whl` | `62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775` |
| `certifi-2026.7.22.tar.gz`（锁保留上游 hash，候选仅允许 wheel） | `741e2c3b351ddf169a738da9f2c048608ff7f2c5cc02f1ebc6b118bb090d5d55` |
| wheel 内 `certifi/cacert.pem`（240216 字节、121 个 PEM 证书） | `9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f` |

`monitor.spec` 收集 certifi 数据及 distribution metadata/LICENSE。离线 self-test 再核对上述 CA 摘要，要求显式包内和默认 SSL 上下文具有相同且非空的根证书集合，并报告版本、摘要、根证书数和验证开关；不向模型或其他远程 HTTPS 服务发送测试请求。本地测试动态生成临时 CA 与服务器证书，仅监听 loopback，使用真实 urllib 默认 HTTPS 上下文验证合法证书/主机名成功、错误主机名和不受信任证书失败，以及损坏 CA、包外回退、版本错误和源码行为边界。测试证书及私钥只在临时目录内存在并清理。

此次完整 uv 重解析新增 certifi，逐项对比原锁确认其余 31 个版本与 826 个 hash 原样保留。该源码测试不替代 root 随后的冻结包重建、移动路径自检和归档验收。

## 冻结候选发现：OpenCV 与解释器的 OpenSSL 同名冲突

候选 3 的冻结诊断在导入 `ssl` 时失败：`_ssl.cpython-311-darwin.so` 需要 `_X509_STORE_get1_objects`，而其实际装载的 `cv2/.dylibs/libcrypto.3.dylib` 没有该符号。`Analysis-00.toc` 的根 `libssl.3.dylib`、`libcrypto.3.dylib` 均是指向 OpenCV wheel 内副本的 `SYMLINK`。已对照实际安装的 PyInstaller 6.22.3 源码：依赖分析会建立这些根别名，`normalize_toc` 中 `SYMLINK` 优先于 `BINARY`，macOS 重定位又将非系统依赖统一为 `@rpath/<basename>`，因此单独添加正确 binary 仍可能被同名别名覆盖。[官方固定版本依赖分析](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/depend/bindepend.py)、[TOC 规则](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/building/datastruct.py)、[macOS 重定位](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/utils/osx.py)

`monitor.spec` 现从构建解释器 `_ssl` 与 `_hashlib` 的真实 Mach-O 依赖解析 OpenSSL 3 库，要求两模块及 libssl 自身引用同一 libcrypto；显式加入 Analysis 的 `binaries` 以保留递归依赖分析。Analysis 结束后，仅将这两个已知根别名替换为已解析的解释器库 `BINARY`，保留 OpenCV 的原始库和其他 native 依赖。未知冲突或缺失库直接失败，不修改 venv、不关闭架构、TLS、FaceMesh 或 runtime digest 验证。[官方显式 binary 收集](https://pyinstaller.org/en/v6.22.3/spec-files.html#adding-binary-files)

由于 PyInstaller 的统一 basename 重定位，最终 OpenCV 的相关依赖也会解析到根目录中的这对 OpenSSL 3 库；本修复不声称为同名库保留两套独立动态加载命名空间。OpenSSL 对同一 major 的后续 minor 规定保持 ABI；仍需冻结后 TLS 与 FaceMesh、实际 dyld 闭包及移动目录 smoke 共同证明本候选可用。[OpenSSL 版本与 ABI 规则](https://openssl-library.org/policies/general/versioning-policy/)

2026-10-13 的本机工具链证据（不表示完整 hermetic 构建或正式来源证明）：

| 输入 | 实测值 |
| --- | --- |
| 解释器 | Homebrew CPython 3.11.16 |
| `_ssl` | `/opt/homebrew/Cellar/python@3.11/3.11.16/Frameworks/Python.framework/Versions/3.11/lib/python3.11/lib-dynload/_ssl.cpython-311-darwin.so` |
| SSL 运行版本 | `OpenSSL 3.6.4 25 Aug 2026` |
| libssl 来源 | `/opt/homebrew/Cellar/openssl@3/3.6.4/lib/libssl.3.dylib` |
| libssl SHA-256 | `a53b324db78c1146ff9ce68700f952e3997f4a1965ee6c6a802f6d3bdcd625ad` |
| libcrypto 来源 | `/opt/homebrew/Cellar/openssl@3/3.6.4/lib/libcrypto.3.dylib` |
| libcrypto SHA-256 | `48c160c3aaa46cb69e5874370d820c2bc8be0712f6ee690fa10581ad966474a7` |

新增四项 spec 回归与十项既有 TLS 测试均通过；另在实际 PyInstaller 6.22.3 环境执行 `normalize_toc`，重现旧 root alias 胜出，再确认修复后选择解释器 `BINARY` 且保留 OpenCV 原始条目。候选 4 的真实重建由独立构建步骤执行，本段不预先宣告其成功；所有候选 self-test 明确 `releaseEligible:false`。

候选 4 又确认 Analysis 最终会将归一化后的 `SYMLINK` 移入 `a.datas`，所以只检查 `a.binaries` 会按设计失败，未产生误报成功的候选。修复进一步以两份实际最终 TOC 联合核验，仅从两处移除已知根别名，把解释器 pair 明确放回 `a.binaries`；其他 data/binary 保持原样。回归夹具相应使用“OpenCV 原库在 binaries、root alias 在 datas”的真实分区，候选 5 执行完整冻结及移动路径验收。[官方 Analysis 最终分类](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/building/build_main.py)

候选 5 已通过构建并越过 OpenSSL import 错误，但诊断因 certifi 模块版本 `2026.07.22` 与 distribution metadata 的规范形式 `2026.7.22` 直接字符串比较而失败。现使用已锁定的 `packaging.version.Version` 同时核对两种版本来源，继续要求固定版本与 CA 摘要完全匹配。包内 PYZ 模块的 `__file__` 是虚拟源路径，因此仅要求其解析位置在 bundle 内；实际 CA 文件仍必须存在且通过摘要核验。15 项 TLS/spec 测试通过，并用真实隔离安装的 certifi 验证两种版本、CA 摘要和 121 个根证书；候选 6 再执行完整冻结验收。[certifi 实际模块版本](https://github.com/certifi/python-certifi/blob/master/certifi/__init__.py)、[标准版本比较](https://packaging.pypa.io/en/stable/version.html)、[PyInstaller 的模块路径规则](https://pyinstaller.org/en/v6.22.3/runtime-information.html#using-file)

最终候选 7 完成全部构建门禁。候选 6 暴露的HTTP helper双执行由调用方修正Node参数位置，保留helper原合同；真实Node回归先复现后通过。原位及中文／空格迁移均通过合成FaceMesh／JPEG、TLS和HTTP/SSE；加载的142个非系统native库均在冻结目录，OpenSSL为3.6.4、根证书为121个。两次stage摘要均为 `d890ecc5af58d7c275fedbff41ff035eee456ec9bb9445269f6652cb25a51db3`，这是本地诊断证据，不是可信Release来源；上述MediaPipe元数据及平台／分发限制不因运行成功而消除。
