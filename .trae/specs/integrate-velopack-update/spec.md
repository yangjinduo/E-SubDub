# 集成 Velopack 自动更新（GitHub Releases 更新源）Spec

## Why

软件已发布 GitHub（https://github.com/yangjinduo/E-SubDub），需要自动更新能力。选用开源方案 Velopack（https://velopack.io/），以 GitHub Releases 作为更新源。当前 Inno 扁平安装布局（程序与运行期数据同目录）与 Velopack 的 `current` 目录整体替换机制冲突，需重构目录推导；同时借机修正"输出目录默认落在程序文件夹下"的不合理设计。

## 已确认的决策（会话中与用户逐项确认，新会话勿再询问）

1. **数据目录位置（冻结态）**：models、tools、third_party、.venv-ml、.venv-tts 放在 Velopack 安装根目录下、与 `current` 同级。用户明确接受"卸载时随安装根整体删除"（卸载即不想要这些大文件）。
2. **数据目录位置（开发态）**：放在项目根的**上一级**（`20261029-E-SUBDUB\`），镜像冻结态布局。
3. **输出目录**：取消默认值，改为用户必填 + 持久化保存（详见 R3）。
4. **更新方式**：启动时后台自动检查 + 提示；另在设置窗口（`app/ui/dialogs/resource_dialog.py`）"主题"下拉右侧增加「检查更新」按钮手动触发。
5. **不做代码签名**。
6. **不做老用户迁移**（项目尚无用户，测试阶段），但须在文档中写明开发机存量数据文件夹的搬移指引。
7. **首装包形态**：沿用用户另一项目的"Inno 向导包装 Velopack Setup.exe"混合方案（参考样板 `test/build_windows.ps1` 与 `test/installer_velopack.iss` 即 `test/installer_windows_velopack.iss`），保留现有 Inno 向导的磁盘空间检测/分区选择能力，经 `Setup.exe --installto "{app}" --silent` 落盘到用户所选目录。
8. PackId 定为 `yangjinduo.E-SubDub`，channel 用默认 `win`。

## What Changes

- **目录布局重构**：`app/core/config.py` 的 `_resolve_roots()` 改为三态推导（详见 R1）。
- **应用内更新客户端**：新增 `velopack` 依赖与 `app/core/updater.py` 模块；`app/main.py` 入口最先执行 `velopack.App().run()`；启动后台检查 + 设置窗口手动检查按钮。
- **输出目录 UX**：`app/ui/widgets/task_settings.py` 两个面板的输出目录默认值清空，改为持久化的"上次输出目录"；任务开始时为空则弹目录选择并保存。
- **构建链路**：`build/build_windows.ps1` 增加 vpk pack 与 Inno 包装步骤；新增 `build/installer_velopack.iss`（替代旧 `build/installer.iss`）。
- **CI**：`.github/workflows/release.yml` 增加 vpk download/pack/upload 步骤与 tag/APP_VERSION 一致性校验，移除 softprops 上传步骤。
- **文档**：README 更新构建前置条件（.NET SDK + vpk）、目录布局说明、开发态数据迁移指引。
- **BREAKING**（构建侧）：冻结态数据目录随 Velopack 布局变化；旧 `build/installer.iss` 流程废弃；开发态数据目录从项目根移至上一级（需手动搬移存量数据）。

## Impact

- 受影响代码：
  - `app/core/config.py`（目录推导，核心变更点）
  - `app/main.py`（velopack 启动钩子）
  - `app/core/updater.py`（新增）
  - `app/ui/dialogs/resource_dialog.py`（pref_row 主题右侧加检查更新按钮，约 L157-L166 区域）
  - `app/ui/widgets/task_settings.py`（两面板输出目录，L118 与 L300 的 `QLineEdit(str(config.OUTPUT_DIR))` 默认值）
  - 任务创建/开始校验处（`app/ui/dialogs/` 下任务窗口，输出目录为空时弹提示）
  - 设置持久化（`app/core/task_store.py` / i18n 配置目录的 tasks.json，新增 `output_dir` 键）
  - `app/core/i18n.py`（新增文案的 zh/en 词条）
  - `requirements.txt`、`build/requirements-freeze.txt`（新增 `velopack`）
  - `build/build_windows.ps1`、新增 `build/installer_velopack.iss`、删除 `build/installer.iss`
  - `.github/workflows/release.yml`
- 已验证事实（供新会话信任，勿重复排查）：全代码库所有数据路径均经 `AppConfig` 类属性派生（`MODELS_DIR`/`TOOLS_DIR`/`THIRD_PARTY_DIR`/`OUTPUT_DIR` 等），无硬编码拼路径，改 `_resolve_roots()` 一处即可全局生效。`RUNTIME_PYTHON_DIR = APP_ROOT/runtime/python` 在新布局下指向 `current\runtime\python`，`current` 名称跨更新稳定，venv 的 pyvenv.cfg 依赖它依然成立，无需改动。

## ADDED Requirements

### Requirement: R1 三态目录推导（config.py）

系统 SHALL 按运行形态推导 `(APP_ROOT, DATA_ROOT)`：

1. **冻结态 + Velopack 布局**（真实 exe 位于 `<安装根>\current\` 内，检测方式：`exe 所在目录名 == "current"` 且父目录存在 `Update.exe`）：`APP_ROOT = exe 所在目录`（即 current，程序自身资源如 `app/resources`、`runtime/python` 均在此），`DATA_ROOT = exe 所在目录的父目录`（安装根，models 等数据与 current 同级）。
2. **冻结态非 Velopack**（如 `-SkipInstaller` 产出的裸 onedir 测试布局，exe 直接在发行目录）：沿用旧行为 `APP_ROOT = DATA_ROOT = exe 所在目录`，保证本地裸跑测试不受影响。
3. **开发态**（`python app/main.py`）：`APP_ROOT = 项目根`（E-SubDub 目录，等价于 current），`DATA_ROOT = 项目根的上一级`（`20261029-E-SUBDUB\`，等价于安装根）。

#### Scenario: 冻结态 Velopack 安装后首次运行
- **WHEN** 用户经 Inno 首装包把软件装到 `D:\ESubDub\`（实际为 Velopack 安装根，含 `current\`）后启动软件并下载模型/搭建环境
- **THEN** models、tools、third_party、.venv-ml、.venv-tts 创建于 `D:\ESubDub\` 下（与 `current` 同级），程序资源从 `D:\ESubDub\current\` 读取

#### Scenario: 开发态启动
- **WHEN** 在项目根执行 `python app/main.py`
- **THEN** 数据目录为 `20261029-E-SUBDUB\`（项目根上一级），models 等与 E-SubDub 源码目录同级

#### Scenario: -SkipInstaller 裸 onedir 布局
- **WHEN** 冻结 exe 直接位于发行目录（无 `current` 父结构）
- **THEN** 行为与现状一致（数据与 exe 同目录），本地测试不受影响

### Requirement: R2 应用内更新（启动自动检查 + 手动按钮）

系统 SHALL 集成 Velopack Python 库实现更新：

1. `app/main.py` 中 `velopack.App().run()` 必须先于 QApplication 等一切启动逻辑执行（Velopack 安装/卸载钩子需要）。
2. 更新源为 `GithubSource("https://github.com/yangjinduo/E-SubDub")`（公开仓库，无需 token）。
3. 启动后后台线程静默检查（不阻塞 UI、不弹加载态）；发现新版本时以对话框提示（含版本号与 release notes），用户确认后下载（带进度反馈）并 `apply_updates_and_restart`。
4. 设置窗口（`resource_dialog.py`）语言/主题行（`pref_row`）的"主题"下拉右侧新增「检查更新」按钮：点击即时检查，已是最新则提示，有新版本走同一确认流程。
5. 非 Velopack 安装环境（开发态、裸 onedir）SHALL 优雅降级：启动检查静默跳过，手动按钮提示"当前环境不支持自动更新"类文案，不得抛异常。
6. 所有新文案走 i18n（zh/en 双语）。

#### Scenario: 启动发现新版本
- **WHEN** 已安装 v0.2.0，GitHub Releases 存在更新的 v0.3.0 feed，用户启动软件
- **THEN** 后台检查到更新并弹窗提示；用户确认后下载并自动重启完成升级；升级后 models 等数据目录完整、`.venv-tts`/`.venv-ml` 仍可用

#### Scenario: 手动检查已是最新
- **WHEN** 点击设置窗口「检查更新」且无新版本
- **THEN** 提示当前已是最新版本

### Requirement: R3 输出目录必填与持久化

系统 SHALL 将输出目录改为用户显式设置并持久化：

1. `task_settings.py` 两个面板（TaskSettingsPanel / TtsSettingsPanel）的输出目录输入框默认值清空（不再预填 `config.OUTPUT_DIR`），显示 placeholder 提示文案（i18n）。
2. 上次使用的输出目录持久化到现有用户设置存储（tasks.json 所在的设置体系，新增如 `output_dir` 键）；面板初始化时预填该持久化值（可能为空）。
3. 任务开始/确认时若输出目录为空：弹出提示并打开目录选择对话框；选定后回填输入框并立即持久化；用户取消选择则不开始任务。
4. `config.OUTPUT_DIR` 保留作为浏览对话框的起始定位兜底（及流水线内部兜底），但 UI 不再以它作为默认值展示。

#### Scenario: 首次创建任务未设输出目录
- **WHEN** 用户从未设置过输出目录，配置完任务点击开始
- **THEN** 弹出提示与目录选择框；选定后任务开始，且下次打开任务窗口时输出目录已预填

#### Scenario: 重启后保留
- **WHEN** 用户设置过输出目录后重启软件
- **THEN** 任务窗口输出目录自动预填上次值

### Requirement: R4 本地构建产出 Velopack 更新包 + Inno 首装包

`build/build_windows.ps1` SHALL 在 Nuitka onedir 构建后追加：

1. 定位/校验 vpk（`dotnet tool install -g vpk` 产物，缺失时给出安装指引并中止）。
2. `vpk pack`：`--packId yangjinduo.E-SubDub --packVersion <从 config.py 解析的 APP_VERSION> --packDir <onedir 目录> --mainExe ESubDub.exe --packTitle E-SubDub --channel win --outputDir build\dist\update-feed --icon app\resources\logo\E-SubDub.ico`（无签名参数）。
3. 新增 `build/installer_velopack.iss`：以现有 `build/installer.iss` 的磁盘空间检测/分区选择/每用户安装逻辑为基础，融合 `test/installer_windows_velopack.iss` 的 Velopack 衔接模式——`[Files]` 内嵌 Velopack Setup.exe 至 `{tmp}`，`[Run]` 以 `--installto "{app}" --silent` 静默执行，`Uninstallable=no`（卸载由 Velopack Update.exe 负责），装完可运行 `current\ESubDub.exe`。
4. 把 Inno 首装包拷贝覆盖更新 feed 目录中的 `yangjinduo.E-SubDub-win-Setup.exe`（发布出去的首装入口始终是带向导的 Inno 包，参照 test 脚本 L683 的做法）。
5. `-SkipInstaller` 开关语义扩展为跳过 vpk + Inno 全部步骤（只产裸 onedir）。

#### Scenario: 本地完整构建
- **WHEN** 构建机已具备 .NET SDK、vpk、ISCC，执行 `.\build\build_windows.ps1`
- **THEN** 产出 onedir 目录、`build\dist\update-feed\`（releases.win.json + 全量/增量 nupkg + 被首装包替换的 Setup.exe）与 Inno 首装包

### Requirement: R5 CI 发布到 GitHub Releases（含增量包）

`.github/workflows/release.yml` SHALL：

1. 保留现有触发（`v*` tag / 手动）、Python/Nuitka/便携 CPython/ISCC 各步骤。
2. 新增：安装 vpk（`dotnet tool install -g vpk`，windows-latest 自带 .NET SDK）。
3. 新增：构建前执行 `vpk download github --repoUrl https://github.com/yangjinduo/E-SubDub --outputDir build\dist\update-feed` 拉取上一版包（生成增量 delta 所需；无历史版本时优雅处理）。
4. 新增版本一致性校验步骤：从 `app/core/config.py` 解析 `APP_VERSION`，若与触发 tag（去掉 v 前缀）不一致则构建失败。
5. 构建后用 `vpk upload github --repoUrl https://github.com/yangjinduo/E-SubDub --publish --tag v<版本> --outputDir build\dist\update-feed` 发布（替代 softprops/action-gh-release 步骤，删除之）。
6. 保留 onedir 归档 artifact 步骤。

#### Scenario: 推送 v0.2.0 标签
- **WHEN** APP_VERSION 已改为 0.2.0 并推送 tag v0.2.0
- **THEN** CI 产出含全量与增量包的 GitHub Release；已安装 v0.1.x-Velopack 版的客户端可检测并增量升级

### Requirement: R6 文档更新（README）

README SHALL 更新：构建安装包章节补充 .NET SDK + `dotnet tool install -g vpk` 前置条件与 vpk/Inno 双产物说明；「构建安装包」「文件夹分类」章节的目录布局描述改为 Velopack 新布局；快速开始补充开发态数据目录位置说明。

## REMOVED Requirements

### Requirement: 旧 Inno 直装流程
**Reason**: 被 Inno 包装 Velopack 的混合方案（R4）取代。
**Migration**: `build/installer.iss` 删除，其空间检测/分区选择逻辑移植入 `build/installer_velopack.iss`；`release.yml` 中对应上传步骤由 vpk upload 取代。

## 附：开发机存量数据迁移指引（须写入 README，并在此留档）

开发态数据目录从项目根（`E-SubDub\`）移至上一级（`20261029-E-SUBDUB\`）。需手动搬移以下文件夹（从 `E-SubDub\` 移到 `20261029-E-SUBDUB\`）：

- `models\`（模型，约 15GB+）
- `tools\`
- `third_party\`
- `.venv-ml\`、`.venv-tts\`（搬移后 venv 内绝对路径可能失效，失效则在设置窗口删除并一键重建，软件本身支持自动重建）
- `output\`（历史任务产物，可选搬移；新版本起输出目录由用户显式设置）

**不搬移**（属于程序自身文件，留在项目根）：`app\`、`libs\`、`torchaudio\`（垫片包）、`venv\`（主开发环境）、`build\`、`test\` 等。
