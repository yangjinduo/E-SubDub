# Minim SubDub

![Minim SubDub](app/resources/logo/Minim%20SubDub.png)

**Minim SubDub** —— 极简的视频字幕与 AI 配音生成工具。

> **名称由来**：Minim = minimal（极简）；SubDub = Subtitle + Dubbing（字幕 + 配音）。

- 版本：v0.1.0
- 平台：Windows / macOS
- 界面语言：简体中文 / English

## 功能特性

- **三类任务入口**：任务列表顶部提供三个独立入口——**＋配音与字幕**（完整流程）、**＋生成字幕**（仅到译文审核）、**＋文本转语音**（纯 TTS）。
- **配音与字幕**：导入视频，选择源语言与目标语言，自动完成从音频提取到成品封装的完整流程。
- **文本转语音（纯 TTS）**：输入或拖入 TXT 文本，选择参考音频克隆音色，逐句合成语音；支持文本语言自动识别（langid）、语速 0.5–2.0 倍、逐句情绪定制、输出 WAV / MP3 / M4A / FLAC，并可按每句实际时长同步生成 SRT 字幕。
- **说话人识别转写**：基于 MOSS-Transcribe-Diarize，自动区分说话人并生成带时间轴的分句文本。
- **针对中文特别优化的翻译**：
  - 使用 Index-Translate-2B 模型翻译字幕；
  - 中文源语言额外启用 Index-Homura-2B 时长修正——按原语言每个句子的时长生成时长接近的译文，让配音与原片口型节奏对齐。
- **声音克隆配音**：基于 IndexTTS-2.5，从原片人声自动挑选参考音色，按说话人克隆音色合成配音，并支持时长伸缩对齐。
- **人工复核，或跳过复核批量执行**：
  - 文本审核：校对 ASR 转写结果；
  - 译文复核：修改翻译结果（自动同步回 SRT 字幕）；
  - 配音复核：逐句试听、修改译文并重新生成配音；
  - 也可跳过全部人工审核，执行批量任务。
- **断点续跑**：每个任务的工作目录以 manifest.json 记录各阶段状态；执行到一半的任务，下次导入相同路径下的文件时自动跳过已完成阶段、从断点继续。
- **双语字幕**：生成源语言 + 目标语言双语 SRT 字幕文件。
- **资源自动管理**：模型经魔搭社区（ModelScope）自动下载；TTS 虚拟环境与 llama.cpp 推理服务可一键搭建；启动时自动检测 CUDA / MPS / CPU 推理设备。

## 快速开始

### 环境要求

- Python 3.13
- NVIDIA GPU（可选，CUDA 加速；无 GPU 时自动回退 CPU，速度较慢）
- 磁盘空间：模型合计约 10+ GB

### 安装与启动

```bash
# 1. 创建虚拟环境并安装依赖（Windows 示例）
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 注意：torch 2.14 无配套 torchaudio，项目根目录的 torchaudio/ 垫片包
# 用于替代，切勿 pip install torchaudio

# 2. 启动
python app/main.py
```

> **注意**：整个应用始终在主虚拟环境（`venv/`）中启动运行；
> `.venv-tts/` 仅由 TTS worker 子进程在合成时自动调用，**不要**用它启动 `app/main.py`。

首次启动后：

1. 在主界面右上角 **设置** 中下载所需模型（ASR / 翻译 / 人声分离 / 配音）；
2. 在设置窗口 **运行环境** 行搭建 TTS 虚拟环境（.venv-tts）与 LLAMA 推理服务（llama.cpp）；
3. 点击任务列表顶部的任务按钮创建任务：
   - **＋ 配音与字幕**：导入视频，配置源/目标语言，执行完整流程；
   - **＋ 生成字幕**：同上，但只产出字幕（无人声分离与配音阶段）；
   - **＋ 文本转语音**：输入 TXT 文本 + 参考音频，克隆音色合成语音。

### 使用流程

```
导入视频 → 创建任务（配音与字幕 / 生成字幕）→（可选）人工复核 → 成品输出
输入文本 + 参考音频 → 创建任务（文本转语音）→ 成品输出
```

任务队列支持批量执行；中途取消或失败的任务，重新导入同路径文件即可断点续跑。

## 流水线阶段

每个任务在工作目录（默认 `output/`）下按流程顺序分阶段落盘：

| #  | 阶段   | 目录        | 说明                                       |
| -- | ---- | --------- | ---------------------------------------- |
| 01 | 音频提取 | `01_音频提取` | FFmpeg 提取完整音轨                            |
| 02 | 人声分离 | `02_人声分离` | Demucs 分离人声 / 伴奏                         |
| 03 | 语音识别 | `03_语音识别` | 说话人分离 + 分句转写                             |
| 04 | 文本审核 | `04_文本审核` | 人工校对转写文本（可跳过）                            |
| 05 | 翻译   | `05_翻译`   | Index-Translate-2B 翻译（中文源启用 Homura 时长修正） |
| 06 | 译文审核 | `06_译文审核` | 人工修改译文（可跳过）                              |
| 07 | 配音生成 | `07_配音生成` | 参考音色挑选 + IndexTTS-2.5 声音克隆合成             |
| 08 | 配音复核 | `08_配音复核` | 逐句试听 / 重生成（可跳过）                          |
| 09 | 混音   | `09_混音`   | 配音与背景音混合                                 |
| 10 | 封装输出 | `10_封装输出` | 成品归档：MKV 封装（音轨名 `AI dubbing {语言}`）+ 双语 SRT；纯字幕任务为 SRT 副本 |

**生成字幕**任务只执行 01 / 03 / 04 / 05 / 06 / 10 阶段（无人声分离与配音相关阶段，成品 SRT 归档至 `10_封装输出`）。

**文本转语音**任务使用独立的三阶段流程（同名工作目录）：

| #  | 阶段   | 目录         | 说明                                             |
| -- | ---- | ---------- | ----------------------------------------------- |
| 01 | 文本处理 | `01_文本处理` | 文本分句、语言检测（langid）、情绪定制结果落盘                    |
| 02 | 语音合成 | `02_语音合成` | IndexTTS-2.5 按参考音频克隆音色逐句合成（含语速与情绪向量）        |
| 03 | 封装输出 | `03_封装输出` | 拼接句间静音，按任务名直写 WAV / MP3 / M4A / FLAC 成品，按实际时长生成 SRT |

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
│   ├── tts_runner.py        # 文本转语音流水线引擎（分句 → 合成 → 封装输出）
│   ├── langid_util.py       # 文本语言自动识别（langid，缺失时优雅降级）
│   └── task_manager.py      # 任务队列：线程生命周期、任务窗口管理
├── pipeline/                # 流水线各阶段实现
│   ├── audio_extract.py     # 01 音频提取
│   ├── separate.py          # 02 人声分离（Demucs）
│   ├── asr.py               # 03 语音识别（MOSS-Transcribe-Diarize + VAD 切块）
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
│   ├── env_setup.py         # 运行环境搭建（.venv-tts、llama.cpp 下载）
│   └── logo/                # 应用 LOGO（.ico / .icns）
└── ui/                      # PySide6 界面
    ├── main_window.py       # 主窗口：资源状态栏 + 任务列表（三类任务入口）+ 系统监视
    ├── style.py             # 全局 QSS 样式
    ├── widgets/             # 任务列表、资源状态栏、导入区、任务设置、TTS 任务设置、
    │                        #   分段表格、音频播放器、流程面板、系统监视
    └── dialogs/             # 三类任务窗口、设置、文本/译文/配音复核、
                             #   音色管理、TTS 情绪定制、阶段结果查看器
```

**关键设计**：

- **阶段化落盘 + manifest 断点续跑**：每阶段产物固定子夹 + 完成标记，重复执行同文件任务时跳过已完成阶段；人工修改内容后由 runner 显式失效下游阶段。
- **TTS 独立虚拟环境**：IndexTTS-2.5 与 MOSS-Transcribe-Diarize 所依赖的 transformers 版本不兼容，故 TTS 运行于独立的 `.venv-tts` 子进程中，经 `tts_worker` 通信；模型跨「生成 / 复核」两阶段复用，复核结束统一释放。
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

| 分类 | 目录 | 说明 |
|------|------|------|
| 运行必须 | `app/` | 全部源代码 |
| 运行必须 | `libs/` | 内置 FFmpeg（windows/ffmpeg.exe、macos/Arm + Intel） |
| 运行必须 | `tools/llama.cpp/` | llama-server 推理服务 + CUDA DLL（可软件自动下载） |
| 运行必须 | `third_party/index-tts/` | IndexTTS 引擎源码 |
| 运行必须 | `torchaudio/` | torch 2.14 配套垫片包（demucs 依赖，勿 pip 安装覆盖） |
| 运行必须 | `.venv-tts/` | TTS 专用虚拟环境（可软件自动重建） |
| 运行必须 | `models/` | 五个模型（可软件自动下载，已 gitignore） |
| 运行时生成 | `output/` | 任务工作目录与成品（gitignore） |
| 开发用 | `venv/` | 主开发虚拟环境 |
| 开发用 | `requirements.txt`、`.gitignore`、`安装引导打包阶段注意事项.TXT` | 依赖清单 / 打包阶段备忘 |
| 测试 | `test/` | e2e 冒烟、TTS 探针脚本 + 测试视频（gitignore） |

## 声音克隆使用声明

本软件的声音克隆功能（视频配音与文本转语音）仅限在您拥有原音频声音合法使用权利的前提下使用，生成的配音与语音仅可用于合法用途。首次使用时软件将弹出授权确认。

## 许可证

Apache 2.0
