# E-SubDub

[English](README-EN.md) | 简体中文

![E-SubDub](app/resources/logo/E-SubDub.png)

**E-SubDub** —— 简单易用的视频字幕与 AI 配音生成工具。

> **名称由来**：E = Easy（容易，与中文“易”同音，操作简单）；SubDub = Subtitle + Dubbing（字幕 + 配音）。

- 版本：v0.1.0
- 平台：Windows / macOS
- 界面语言：简体中文 / English

### 生成效果演示
- [演示视频1](https://www.bilibili.com/video/BV19ea76RE1A/?spm_id_from=333.1387.homepage.video_card.click&vd_source=de34c79a1e39f39a29a1f3ea3b98cad7)
- [演示视频2](https://www.bilibili.com/video/BV1t1a767ECH/?spm_id_from=333.1387.homepage.video_card.click&vd_source=de34c79a1e39f39a29a1f3ea3b98cad7)

## 功能特性

- **三类任务入口**：任务列表顶部提供三个独立入口——**＋配音与字幕**（完整流程）、**＋生成字幕**（仅到译文审核）、**＋文本转语音**（纯 TTS）。
- **配音与字幕**：导入视频，选择源语言与目标语言，自动完成从音频提取到成品封装的完整流程。
- **文本转语音（纯 TTS）**：输入或拖入 TXT 文本，选择参考音频克隆音色，逐句合成语音；支持文本语言自动识别（langid）、语速 0.5–2.0 倍、逐句情绪定制、输出 WAV / MP3 / M4A / FLAC，并可按每句实际时长同步生成 SRT 字幕。合成后进入语音复核：试听 / 连续试听、删除句子、点击情绪列直接更换情绪并即时重生成。
- **说话人识别转写**：基于 MOSS-Transcribe-Diarize，自动区分说话人并生成带时间轴的分句文本。
- **针对中文特别优化的翻译**：
  - 使用 Index-Translate-2B 模型翻译字幕；
  - 中文源语言额外启用 Index-Homura-2B 时长修正——按原语言每个句子的时长生成时长接近的译文，让配音与原片口型节奏对齐。
- **声音克隆配音**：基于 IndexTTS-2.5，从原片人声自动挑选参考音色，按说话人克隆音色合成配音，并支持时长伸缩对齐。
- **人工复核，或跳过复核批量执行**：
  - 文本审核：校对 ASR 转写结果；
  - 译文复核：修改翻译结果（自动同步回 SRT 字幕）；
  - 配音复核：逐句试听、修改译文并重新生成配音；
  - 语音复核（文本转语音）：试听 / 连续试听、删除句子、点击情绪列更换情绪并即时重生成；
  - 也可跳过全部人工审核，执行批量任务。
- **任务队列与断点续跑**：每个任务的工作目录以 manifest.json 记录各阶段状态；任务列表持久化（重启自动恢复），主窗口"继续"按钮一键把已中断 / 失败 / 已取消的任务重新入列，"停止"按钮中止整个队列（不影响任务窗口内"停止"仅停当前任务、队列继续的语义）。
- **流程结果查看**：点击任务窗口中已完成的流程卡片，即可打开该阶段的结果查看窗口——分段表格试听、逐句合成结果、成品封装预览（原视频 / 配音视频对照播放 + 双语字幕面板）等，并支持从该流程一键重跑。
- **双语字幕**：生成源语言 + 目标语言双语 SRT 字幕文件；预览面板中每条字幕左侧显示起止时间，并随播放进度自动高亮滚动。
- **资源自动管理**：模型经魔搭社区（ModelScope）自动下载；ML 推理环境（安装包形态为 `.venv-ml`）、TTS 虚拟环境（`.venv-tts`）与 llama.cpp 推理服务可一键搭建；启动时自动检测 CUDA / MPS / CPU 推理设备。

## 快速开始

### 环境要求

- Python 3.13
- NVIDIA GPU（可选，CUDA 加速；无 GPU 时自动回退 CPU，速度较慢）
- 磁盘空间：约 20+ GB

### 安装与启动

```bash
# 1. 创建虚拟环境并安装依赖（Windows PowerShell 示例；cmd 用户激活改用 venv\Scripts\activate.bat）
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 若 Activate.ps1 报"禁止运行脚本"（执行策略限制），先执行一次再重新激活：
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 注意：torch 2.14 无配套 torchaudio，项目根目录的 torchaudio/ 垫片包
# 用于替代，切勿 pip install torchaudio

# 2. 启动
python app/main.py
```

> **注意**：整个应用始终在主虚拟环境（`venv/`）中启动运行；
> `.venv-tts/` 与 `.venv-ml/`（安装包形态的 ML 推理环境）仅由对应 worker 子进程自动调用，**不要**用它们启动 `app/main.py`。

> **目录布局（开发态）**：`models/`、`tools/`、`third_party/`、`.venv-tts/`、`.venv-ml/` 等大体积数据目录位于**项目根的上一级**（与 `E-SubDub/` 源码目录同级），镜像安装版的 [Velopack](https://velopack.io/) 布局（安装根下，数据与 `current/` 程序目录平级，跨自动更新保留）。首次启动自动创建。
>
> **从旧版本升级的开发者**：请把上述目录（`output/` 可选）从项目根手动搬移到上一级；`.venv-*` 搬移后如内部绝对路径失效，在设置窗口删除并一键重建即可（软件支持自动重建）。`app/`、`libs/`、`torchaudio/`、`venv/`、`build/`、`test/` 属于程序自身或开发文件，**不搬移**。

首次启动后：

1. 在主界面右上角 **设置** 中下载所需模型（ASR / 翻译 / 人声分离 / 配音）；
2. 在设置窗口 **运行环境** 行搭建 TTS 虚拟环境（.venv-tts）与 LLAMA 推理服务（llama.cpp）——源码运行时 ML 推理（人声分离 / 语音识别）直接复用主虚拟环境，仅安装包形态会自动搭建独立的 ML 推理环境（.venv-ml）；
3. 点击任务列表顶部的任务按钮创建任务：
   - **＋ 配音与字幕**：导入视频，配置源/目标语言，执行完整流程；
   - **＋ 生成字幕**：同上，但只产出字幕（无人声分离与配音阶段）；
   - **＋ 文本转语音**：输入 TXT 文本 + 参考音频，克隆音色合成语音。

### 使用流程

```
导入视频 → 创建任务（配音与字幕 / 生成字幕）→（可选）人工复核 → 成品输出
输入文本 + 参考音频 → 创建任务（文本转语音）→ 成品输出
```

任务队列支持批量执行；任务列表自动持久化，重启后可恢复。中途取消或失败的任务，可在主窗口点击"继续"一键重新入列（也可重新导入同路径文件断点续跑）。

## 构建安装包（Windows）

打包前的准备（构建机，一次性）：

1. Python 3.13 构建虚拟环境（Nuitka 必须钉 4.2.2：4.3 在 PySide6 6.9.3 冻结态触发确定性崩溃，详见 `build\build_windows.ps1` 头部注释）：

```bash
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r build\requirements-freeze.txt nuitka==4.2.2
```

2. .NET SDK + vpk（[Velopack](https://velopack.io/) 打包 CLI，缺失时构建脚本打印安装指引并中止；增量更新包与 GitHub Releases 自动更新依赖它）：

```bash
winget install Microsoft.DotNet.SDK.8
dotnet tool install -g vpk
```

3. （可选）Inno Setup 6——缺失时跳过 Inno 首装包步骤（更新 feed 保留 vpk 原生 Setup.exe）；ISCC 从 PATH 与标准安装位置自动探测：

```bash
winget install -e --id JRSoftware.InnoSetup
```

4. C 编译器：优先 MSVC（VS Build Tools 的 VCTools）；缺失时 Nuitka 经 `--assume-yes-for-downloads` 自动下载 MinGW64，二选一即可。

5. 便携 CPython（[python-build-standalone](https://github.com/astral-sh/python-build-standalone)，随安装包分发给最终用户，部署期 `.venv-ml` / `.venv-tts` 以它为基础解释器）：手动下载 [cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20250723/cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz)，用 Win10+ 自带 tar 解压到 `build\runtime\`：

```bash
tar -xzf cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz -C build\runtime
```

解压后应位于 `build\runtime\python\python.exe`。它是标准便携发行版，对任何 x64 Windows 机器通用，从其他机器拷贝亦可。

打包方法（在项目根、已激活构建 venv 的终端中）：

```powershell
.\build\build_windows.ps1                # 完整构建（onedir + Velopack 更新 feed + Inno 首装包）
.\build\build_windows.ps1 -SkipInstaller # 只产裸 onedir（跳过 vpk pack 与 Inno 全部步骤，本地冻结态测试用）
```

产物（版本号的唯一真值源是 `app/core/config.py` 的 `APP_VERSION`，脚本自动从该文件解析）：

| 产物 | 说明 |
| --- | --- |
| `build\dist\ESubDub\` | onedir 发行目录 |
| `build\dist\update-feed\` | Velopack 更新 feed：`releases.win.json` + 全量/增量 nupkg + `yangjinduo.E-SubDub-win-Setup.exe`（该 Setup.exe 已被 Inno 首装包覆盖，发布出去的首装入口始终是带向导的 Inno 包） |
| `build\dist\ESubDub-Setup-<版本>.exe` | Inno 首装包：向导式包装 Velopack Setup.exe（磁盘空间检测 / 分区选择 / 每用户安装），经 `--installto` 静默落盘到用户所选目录 |

### 发版流程（每次出新版本）

发版前修改两个文件（版本相关内容的全部落点）：

| 文件 | 修改内容 |
| --- | --- |
| `app\core\config.py` | `APP_VERSION` 改为新版本号（如 `"0.1.1"`），全链路唯一真值源 |
| `build\release-notes.md` | **整文件重写**为当版更新说明（勿追加旧版内容：vpk 把整个文件内嵌进更新包，客户端「检查更新」弹窗与 GitHub Release 正文原样展示）；本地备注用 HTML 注释 `<!-- -->` 包裹，构建时自动剔除、不随包发布 |

**方式一：云端发布（推荐）** —— 修改 commit 并 push 后打 tag，触发 `.github/workflows/release.yml` 自动完成构建与发布：

```bash
git tag v0.1.1
git push origin v0.1.1
```

CI 校验 tag 与 `APP_VERSION` 一致（不一致直接失败）、`vpk download github` 拉取上一版生成增量包、构建后 `vpk upload github --publish` 发布 Release。注意 `release-notes.md` 必须**先提交再打 tag**（CI 只能读到已 push 的内容）。

若 CI 构建失败：修复、commit、push 之后，删除旧 tag 重新打（tag 必须指向包含修复的新提交；若失败前 Release 已半生成，先在 GitHub 网页上删掉它）：
例如：
```bash
git tag -d v0.1.1                 # 删除本地 tag
git push origin :refs/tags/v0.1.1 # 删除远端 tag（触发不了任何构建）
git tag v0.1.1                    # 在当前已含修复的提交上重新打
git push origin v0.1.1            # 重新触发 CI
```

**方式二：本地构建发布** —— 项目根、已激活构建 venv 的终端：

```powershell
# 1.（可选）拉取上一版到 feed 目录，供 vpk pack 生成增量包
vpk download github --repoUrl https://github.com/yangjinduo/E-SubDub --outputDir build\dist\update-feed

# 2. 完整构建
.\build\build_windows.ps1

# 3. 发布到 GitHub Release（token 需有写权限，经 --token 传入——
#    vpk 不读 GITHUB_TOKEN 环境变量；或改用手动网页上传）
$token = '<你的 GitHub Token>'
vpk upload github --repoUrl https://github.com/yangjinduo/E-SubDub --publish --tag v0.1.1 --outputDir build\dist\update-feed --token $token
```

手动网页上传时，把 `build\dist\update-feed\` 内**全部文件**（`releases.win.json` + 全量/增量 nupkg + `yangjinduo.E-SubDub-win-Setup.exe`）上传到对应 tag 的 Release，覆盖同名旧文件。

GitHub Token 获取（仅方式二的命令行上传需要，网页上传和云端发布都不需要）：打开 https://github.com/settings/personal-access-tokens/new 生成 Fine-grained token——Repository access 只选 `E-SubDub` 仓库，Permissions 里给 **Contents: Read and write**；生成后复制 `github_pat_` 开头的串，发布时贴进上面的 `$token` 变量。个人 token 只在运行命令时临时使用，**切勿写进任何文件提交到仓库**（不慎泄露立即回该页面撤销）。token **不是一次性的**：有效期内（生成时可选，默认 30 天，也可选更长或自定义日期）可反复用于多次发布，到期后重新生成一个即可；发版频率低的话，建议每次现用现生成短有效期 token。

发布后，已安装用户启动时后台自动检查（或设置窗口手动「检查更新」）即可增量升级。安装版布局：安装根下 `current\`（程序，随更新整体替换）与 `Update.exe`（更新器），`models\` 等数据目录与 `current\` 平级、跨更新保留。

## 流水线阶段

每个任务在用户设置的输出目录下按流程顺序分阶段落盘（首次创建任务时选择并记忆，可在任务设置面板修改）：

| #  | 阶段   | 目录        | 说明                                                       |
| -- | ---- | --------- | -------------------------------------------------------- |
| 01 | 音频提取 | `01_音频提取` | FFmpeg 提取完整音轨                                            |
| 02 | 人声分离 | `02_人声分离` | Demucs 分离人声 / 伴奏                                         |
| 03 | 语音识别 | `03_语音识别` | 说话人分离 + 分句转写                                             |
| 04 | 文本审核 | `04_文本审核` | 人工校对转写文本（可跳过）                                            |
| 05 | 翻译   | `05_翻译`   | Index-Translate-2B 翻译（中文源启用 Homura 时长修正）                 |
| 06 | 译文审核 | `06_译文审核` | 人工修改译文（可跳过）                                              |
| 07 | 配音生成 | `07_配音生成` | 参考音色挑选 + IndexTTS-2.5 声音克隆合成                             |
| 08 | 配音复核 | `08_配音复核` | 逐句试听 / 重生成（可跳过）                                          |
| 09 | 混音   | `09_混音`   | 配音与背景音混合                                                 |
| 10 | 封装输出 | `10_封装输出` | 成品归档：MKV 封装（音轨名 `AI dubbing {语言}`）+ 双语 SRT；纯字幕任务为 SRT 副本 |

**生成字幕**任务只执行 01 / 03 / 04 / 05 / 06 / 10 阶段（无人声分离与配音相关阶段，成品 SRT 归档至 `10_封装输出`）。

**文本转语音**任务使用独立的四阶段流程（同名工作目录）：

| #  | 阶段   | 目录        | 说明                                                  |
| -- | ---- | --------- | --------------------------------------------------- |
| 01 | 文本处理 | `01_文本处理` | 文本分句、语言检测（langid）、编辑（情绪）结果落盘                        |
| 02 | 语音合成 | `02_语音合成` | IndexTTS-2.5 按参考音频克隆音色逐句合成（含语速与情绪向量）                |
| 03 | 语音复核 | `03_语音复核` | 试听 / 连续试听、删除句子、点击情绪列更换情绪并即时重生成（可跳过）                 |
| 04 | 封装输出 | `04_封装输出` | 拼接句间静音，按任务名直写 WAV / MP3 / M4A / FLAC 成品，按实际时长生成 SRT |

## 代码架构

```
app/
├── main.py                  # 应用入口：QApplication、全局样式、窗口图标
├── core/                    # 核心逻辑（与 UI 无关）
│   ├── config.py            # 全局配置：应用名 / 版本 / 目录路径 / LOGO
│   ├── i18n.py              # 轻量双语字典 + tr() + 语言偏好持久化
│   ├── hardware.py          # 推理设备检测（CUDA / MPS / CPU）
│   ├── ffmpeg.py            # 内置 FFmpeg 路径定位（按平台）
│   ├── segment.py           # 分段数据结构（句子 / 说话人 / 时间轴）
│   ├── workspace.py         # 工作目录管理：阶段状态 manifest、断点续跑
│   ├── runner.py            # 流水线引擎：按阶段调度、跳过已完成、失效下游
│   ├── tts_runner.py        # 文本转语音流水线引擎（分句 → 合成 → 语音复核 → 封装输出）
│   ├── langid_util.py       # 文本语言自动识别（langid，缺失时优雅降级）
│   ├── task_store.py        # 任务列表持久化：tasks.json 原子写 / 启动恢复
│   └── task_manager.py      # 任务队列：线程生命周期、任务窗口管理
├── pipeline/                # 流水线各阶段实现
│   ├── audio_extract.py     # 01 音频提取
│   ├── separate.py          # 02 人声分离（Demucs）
│   ├── asr.py               # 03 语音识别（MOSS-Transcribe-Diarize + VAD 切块）
│   ├── ml_client.py         # ML 推理引擎：demucs / ASR worker 子进程管理
│   ├── ml_worker.py         # ML 子进程工作端（冻结态运行于 .venv-ml）
│   ├── translate.py         # 05 翻译（含 Homura 时长修正二遍）
│   ├── subtitle.py          # SRT 字幕生成（内部阶段）
│   ├── voice_pick.py        # 07 参考音色挑选
│   ├── tts.py               # 07 配音合成（IndexTTS-2.5，独立 venv 子进程）
│   ├── tts_worker.py        # TTS 子进程工作端
│   ├── mix.py               # 09 混音
│   ├── mux.py               # 10 MKV 封装
│   ├── llm_engine.py        # llama.cpp llama-server 进程管理（翻译推理服务）
│   └── base.py              # 阶段公共基类 / 工具
├── resources/               # 资源管理
│   ├── model.py             # 六项资源注册 / 状态检测 / 删除
│   ├── downloader.py        # 魔搭社区模型下载器
│   ├── env_setup.py         # 运行环境搭建（.venv-ml、.venv-tts、llama.cpp 下载）
│   ├── deploy_log.py        # 部署日志：环境搭建全过程记录（deploy日期.log）
│   └── logo/                # 应用 LOGO（.ico / .icns）
└── ui/                      # PySide6 界面
    ├── main_window.py       # 主窗口：资源状态栏 + 任务列表（三类任务入口）+ 系统监视
    ├── style.py             # 全局 QSS 样式
    ├── widgets/             # 任务列表、资源状态栏、导入区、任务设置、TTS 任务设置、
    │                        #   分段表格、音频播放器、流程面板、字幕面板、系统监视
    └── dialogs/             # 三类任务窗口、设置、文本/译文/配音/TTS 语音复核、
                             #   音色管理、TTS 编辑、阶段结果查看器
```

**关键设计**：

- **阶段化落盘 + manifest 断点续跑**：每阶段产物固定子夹 + 完成标记，重复执行同文件任务时跳过已完成阶段；人工修改内容后由 runner 显式失效下游阶段。
- **任务列表持久化**：tasks.json 以原子写保存于用户配置目录（Windows `%LOCALAPPDATA%\ESubDub\`、macOS `~/Library/Application Support/ESubDub/`），任务列表与全局设置（界面语言、声音克隆授权）共用这一个文件；启动时恢复任务列表，上次运行中 / 排队中的任务标记为"已中断"，等待手动继续。
- **推理环境隔离（双虚拟环境）**：IndexTTS-2.5 与 MOSS-Transcribe-Diarize 所依赖的 transformers 版本（4.x / 5.x）不兼容，不能合用一个环境：TTS 运行于独立 `.venv-tts` 子进程（经 `tts_worker` 通信，torch / numpy 经 .pth 共享主环境或 `.venv-ml`）；安装包形态主程序无 Python 运行时，ML 推理（人声分离 / 语音识别）另建 `.venv-ml` 子进程运行（源码运行直接复用主环境）。TTS 模型跨「生成 / 复核」两阶段复用，复核结束统一释放。
- **LLM 推理外置**：翻译经 llama.cpp 的 llama-server（HTTP）推理，取消任务时立即终止服务进程。
- **线程安全退出**：任务线程退出确认后才释放引用，避免 QThread 提前析构导致的崩溃。

## 模型清单

| 功能        | 模型                          | 来源（魔搭社区）                           |
| --------- | --------------------------- | ---------------------------------- |
| 人声分离      | Demucs（htdemucs）            | DiffSynth-Studio/Demucs-Repackage  |
| 语音识别      | MOSS-Transcribe-Diarize     | openmoss/MOSS-Transcribe-Diarize   |
| 字幕翻译      | Index-Translate-2B          | —                                  |
| 时长修正（中文源） | Index-Homura-2B（Q8\_0 GGUF） | Abu-Dju/Index-Homura-2B-Q8\_0-GGUF |
| 配音合成      | IndexTTS-2.5                | IndexTeam/IndexTTS-2.5             |

## 文件夹分类

大体积数据目录（`..\tools\`、`..\third_party\`、`..\.venv-tts\`、`..\.venv-ml\`、`..\models\`）在开发态位于**项目根的上一级**（`..\` 相对项目根，镜像安装版的 Velopack 布局，见「快速开始」）；安装版则位于安装根、与 `current\` 程序目录平级。

| 分类    | 目录                                                 | 说明                                              |
| ----- | -------------------------------------------------- | ----------------------------------------------- |
| 运行必须  | `app/`                                             | 全部源代码                                           |
| 运行必须  | `libs/`                                            | 内置 FFmpeg（windows/ffmpeg.exe、macos/Arm + Intel） |
| 运行必须  | `..\tools\llama.cpp\`                              | llama-server 推理服务 + CUDA DLL（可软件自动下载）           |
| 运行必须  | `..\third_party\index-tts/`                        | IndexTTS 引擎源码                                   |
| 运行必须  | `torchaudio/`                                      | torch 2.14 配套垫片包（demucs 依赖，勿 pip 安装覆盖）          |
| 运行必须  | `..\.venv-tts/`                                    | TTS 专用虚拟环境（可软件自动重建）                             |
| 运行必须  | `..\.venv-ml/`                                     | ML 推理虚拟环境（安装包形态使用，可软件自动重建）                |
| 运行必须  | `..\models/`                                       | 五个模型（可软件自动下载，已 gitignore）                       |
| 运行时生成 | 输出目录                                              | 任务工作目录与成品（用户设置并持久化记忆，gitignore）                 |
| 开发用   | `build\build_windows.ps1`                          | Windows 一键构建脚本（Nuitka 冻结 + vpk pack + Inno 首装包，见「构建安装包」） |
| 开发用   | `build\installer_velopack.iss`                     | Inno 首装包配置（向导包装 Velopack Setup.exe：空间检查 / 分区选择 + --installto 静默落盘） |
| 开发用   | `build\requirements-freeze.txt`                    | 冻结构建最小依赖清单（不含推理大件）                             |
| 开发用   | `build\runtime\`                                   | 便携 CPython 暂存（手动下载解压，随安装包分发）                  |
| 开发用   | `build\dist\`、`build\work\`                        | 构建产物（含 update-feed 更新 feed）与 Nuitka 中间目录（gitignore）   |
| 开发用   | `venv/`                                            | 主开发虚拟环境                                         |
| 开发用   | `requirements.txt`、`.gitignore`、`安装引导打包阶段注意事项.TXT` | 依赖清单 / 打包阶段备忘                                   |
| 测试    | `test/`                                            | e2e 冒烟、TTS 探针脚本 + 测试视频（gitignore）               |

## 声音克隆使用声明

本软件的声音克隆功能（视频配音与文本转语音）仅限在您拥有原音频声音合法使用权利的前提下使用，生成的配音与语音仅可用于合法用途。首次使用时软件将弹出授权确认。


## 特别感谢

本项目基于诸多优秀的开源模型、工具与框架开发完成，在此向所有开源开发者与社区致以诚挚的感谢！

### 开源模型致谢

- **[Demucs（htdemucs）](https://github.com/adefossez/demucs)**：感谢 Meta 及社区维护者开源的高精度人声分离模型，为本项目音频人声、伴奏分离核心能力提供支撑。

- **[MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize)**：感谢 openmoss 团队开源的带说话人区分的语音识别模型，实现精准的音频转写与分句识别。

- **Index 系列模型**：感谢 IndexTeam、Abu-Dju 等开发者开源的 [Index-Translate-2B](https://github.com/bilibili/Index-Translate)、[Index-Homura-2B](https://modelscope.cn/models/Abu-Dju/Index-Homura-2B-Q8_0-GGUF)、[IndexTTS-2.5](https://github.com/IndexTeam/IndexTTS) 模型，为本项目字幕智能翻译、中文时长适配、高质量声音克隆配音提供核心技术支持。

### 开源工具与引擎致谢

- **[llama.cpp](https://github.com/ggml-org/llama.cpp)**：感谢 llama.cpp 开源项目，提供轻量化、高性能的本地大模型推理服务，支撑翻译模型本地高效推理。

- **[FFmpeg](https://github.com/FFmpeg/FFmpeg)**：感谢 FFmpeg 开源多媒体框架，为项目提供音频提取、视频封装、音视频混音等全流程多媒体处理能力。

### 开发框架与社区致谢

- **[PySide6](https://wiki.qt.io/Qt_for_Python)**：感谢 PySide6 开源 GUI 框架，支撑本项目跨平台桌面客户端界面开发。

- **[PyTorch](https://github.com/pytorch/pytorch) / [Torchaudio](https://github.com/pytorch/audio)**：感谢 PyTorch 深度学习框架及 Torchaudio 音频处理工具库，为模型推理、音频预处理提供基础算力与技术支撑。

- **[魔搭社区（ModelScope）](https://github.com/modelscope/modelscope)**：感谢魔搭社区提供的模型托管、下载加速服务，为用户提供便捷的模型自动部署能力。

- **[langid](https://github.com/saffsd/langid)**：感谢 langid 开源文本语言检测工具，实现多语言文本自动识别适配。

同时感谢所有开源社区的开发者、贡献者，正是开源生态的蓬勃发展，让本项目的落地实现成为可能。

### 开源软件声明

本项目使用、随包分发或运行期部署的全部开源软件、模型及其许可证，统一收录于 [THIRD-PARTY-NOTICES-CN.html](THIRD-PARTY-NOTICES-CN.html)（英文版：[THIRD-PARTY-NOTICES-EN.html](THIRD-PARTY-NOTICES-EN.html)；随安装包分发，应用内页脚「开源声明」入口按界面语言展示对应版本）。该声明用于履行 Apache-2.0 / MIT 等许可证的随附义务，并注明 PySide6（LGPLv3）对应源码的获取方式。依赖版本升级时请同步更新该文件：主程序冻结清单见 `build\requirements-freeze.txt`，子环境清单见 `app\resources\env_setup.py`。

## 未完善功能

目前软件暂未实现视频硬字幕消除功能，该功能可依托 ProPainter、STTN 等视频修复模型，智能去除视频内嵌硬字幕，还原干净视频画面，进一步完善视频本地化配音、字幕制作全流程。​

受限于本人设备显存资源不足，暂无法完成该功能的完整开发与实测适配。特此开源征集各位开发者、爱好者贡献代码，助力功能迭代完善，欢迎大家提交 PR 与 Issue，共同优化项目体验。

## 许可证

Apache 2.0
