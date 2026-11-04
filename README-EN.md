# E-SubDub

English | [简体中文](README.md)

![E-SubDub](app/resources/logo/E-SubDub.png)

**E-SubDub** — An easy-to-use tool for video subtitles and AI dubbing.

> **About the name**: E = Easy (simple to use); SubDub = Subtitle + Dubbing.

- Version: v0.1.0
- Platforms: Windows / macOS
- UI languages: 简体中文 / English

### Demo Videos
- [Demo 1](https://www.bilibili.com/video/BV19ea76RE1A/?spm_id_from=333.1387.homepage.video_card.click&vd_source=de34c79a1e39f39a29a1f3ea3b98cad7)
- [Demo 2](https://www.bilibili.com/video/BV1t1a767ECH/?spm_id_from=333.1387.homepage.video_card.click&vd_source=de34c79a1e39f39a29a1f3ea3b98cad7)

## Features

- **Three task entry points**: The top of the task list provides three independent entries — **＋ Dubbing & Subtitles** (full pipeline), **＋ Generate Subtitles** (up to translation review only), and **＋ Text-to-Speech** (pure TTS).
- **Dubbing & Subtitles**: Import a video, choose source and target languages, and the full pipeline runs automatically from audio extraction to final packaging.
- **Text-to-Speech (pure TTS)**: Type or drop in a TXT file, choose a reference audio to clone a voice, and synthesize speech sentence by sentence; supports automatic language identification (langid), speed from 0.5–2.0×, per-sentence emotion customization, output to WAV / MP3 / M4A / FLAC, and optional SRT subtitles generated from actual sentence durations. After synthesis, a speech review step lets you audition / play continuously, delete sentences, and change emotions by clicking the emotion column with instant re-generation.
- **Speaker-diarized transcription**: Based on MOSS-Transcribe-Diarize, automatically distinguishes speakers and produces sentence-level text with timestamps.
- **Translation specially optimized for Chinese**:
  - Subtitles are translated with the Index-Translate-2B model;
  - For Chinese as the source language, Index-Homura-2B duration correction is additionally enabled — it generates translations whose per-sentence durations match the original, keeping the dubbing aligned with the lip movements of the original video.
- **Voice-cloned dubbing**: Based on IndexTTS-2.5, reference voices are automatically picked from the original vocals, dubbing is synthesized per speaker with cloned voices, and duration stretching keeps it time-aligned.
- **Manual review, or batch execution without review**:
  - Text review: proofread ASR transcription results;
  - Translation review: edit translations (automatically synced back to the SRT subtitles);
  - Dubbing review: audition each sentence, edit translations, and re-generate dubbing;
  - Speech review (Text-to-Speech): audition / play continuously, delete sentences, change emotions from the emotion column with instant re-generation;
  - You can also skip all manual review and run batch tasks.
- **Task queue with checkpoint resume**: Each task's working directory records stage states in manifest.json; the task list is persisted (restored automatically on restart), the "Continue" button in the main window re-queues interrupted / failed / cancelled tasks in one click, and the "Stop" button aborts the whole queue (without affecting the in-task-window "Stop", which stops only the current task while the queue continues).
- **Stage result viewers**: Click a completed stage card in the task window to open its result viewer — segment table audition, per-sentence synthesis results, final packaging preview (original video / dubbed video side-by-side playback + bilingual subtitle panel), etc. — with one-click re-run of that stage.
- **Bilingual subtitles**: Generates an SRT file with source + target languages; the preview panel shows start/end times beside each subtitle and auto-scrolls with playback progress.
- **Automatic resource management**: Models are downloaded automatically from ModelScope; the ML inference environment (`.venv-ml` in the installer build), the TTS virtual environment (`.venv-tts`), and the llama.cpp inference service can be set up with one click; CUDA / MPS / CPU inference devices are detected automatically at startup.

## Quick Start

### Requirements

- Python 3.13
- NVIDIA GPU (optional, CUDA acceleration; automatically falls back to CPU without one, which is slower)
- Disk space: about 20+ GB

### Installation & Launch

```bash
# 1. Create a virtual environment and install dependencies
#    (Windows PowerShell example; cmd users activate with venv\Scripts\activate.bat)
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# If Activate.ps1 reports "running scripts is disabled" (execution policy
# restriction), run the following once, then re-activate:
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Note: torch 2.14 has no matching torchaudio; the torchaudio/ shim package
# in the project root is used instead — do NOT pip install torchaudio

# 2. Launch
python app/main.py
```

> **Note**: The application always starts and runs in the main virtual environment (`venv/`);
> `.venv-tts/` and `.venv-ml/` (the ML inference environment in the installer build) are only
> invoked automatically by their worker subprocesses — do **not** use them to launch `app/main.py`.

After first launch:

1. In **Settings** (top-right of the main window), download the required models (ASR / translation / vocal separation / dubbing);
2. In the settings window, use the **Runtime Environment** row to set up the TTS virtual environment (.venv-tts) and the LLAMA inference service (llama.cpp) — when running from source, ML inference (vocal separation / speech recognition) reuses the main virtual environment directly; only the installer build automatically sets up a separate ML inference environment (.venv-ml);
3. Create tasks with the buttons at the top of the task list:
   - **＋ Dubbing & Subtitles**: import a video, configure source/target languages, and run the full pipeline;
   - **＋ Generate Subtitles**: same as above, but only produces subtitles (no vocal separation or dubbing stages);
   - **＋ Text-to-Speech**: input TXT text + a reference audio, and synthesize speech with a cloned voice.

### Workflow

```
Import video → Create task (Dubbing & Subtitles / Generate Subtitles) → (optional) manual review → Final output
Input text + reference audio → Create task (Text-to-Speech) → Final output
```

The task queue supports batch execution; the task list persists automatically and is restored after restart. Cancelled or failed tasks can be re-queued with one click via "Continue" in the main window (you can also re-import the same file to resume from checkpoints).

## Building the Installer (Windows)

Preparation before packaging (build machine, one-time):

1. A Python 3.13 build virtual environment (Nuitka must be pinned to 4.2.2: 4.3 triggers a deterministic crash in the PySide6 6.9.3 frozen build — see the header comments in `build\build_windows.ps1`):

```bash
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r build\requirements-freeze.txt nuitka==4.2.2
```

2. (Optional) Inno Setup 6 (ISCC on PATH) — if missing, the installer step is skipped automatically and only the onedir distribution is produced:

```bash
winget install -e --id JRSoftware.InnoSetup
```

3. C compiler: MSVC preferred (VCTools from VS Build Tools); if missing, Nuitka automatically downloads MinGW64 via `--assume-yes-for-downloads` — either works.

4. Portable CPython ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), distributed with the installer to end users; `.venv-ml` / `.venv-tts` use it as the base interpreter during deployment): manually download [cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz](https://github.com/astral-sh/python-build-standalone/releases/download/20250723/cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz) and extract it into `build\runtime\` with the tar bundled with Win10+:

```bash
tar -xzf cpython-3.13.5+20250723-x86_64-pc-windows-msvc-install_only_stripped.tar.gz -C build\runtime
```

After extraction it should be at `build\runtime\python\python.exe`. It is a standard portable distribution that works on any x64 Windows machine; copying it from another machine is also fine.

How to package (from the project root, in a terminal with the build venv activated):

```powershell
.\build\build_windows.ps1                # Full build (including installer)
.\build\build_windows.ps1 -SkipInstaller # Produce only the onedir distribution
```

Artifacts: `build\dist\ESubDub\` (onedir distribution), `build\dist\update-feed\` (Velopack update feed: `releases.win.json` + full/delta nupkg + Setup.exe) and `build\dist\ESubDub-Setup-<version>.exe` (Inno Setup installer). The single source of truth for the version number is `APP_VERSION` in `app/core/config.py`; the script parses it from that file automatically.

### Release Workflow (per new version)

Before each release, update two files (the only places that carry version-specific content):

| File | What to change |
| --- | --- |
| `app\core\config.py` | Bump `APP_VERSION` (e.g. `"0.1.1"`); it is the single source of truth for the whole chain |
| `build\release-notes.md` | **Rewrite the whole file** with the notes for this version (do not append older entries: vpk embeds the entire file into the update package and the client dialog shows it verbatim) |

**Option 1: cloud build (recommended)** — commit & push the changes, then tag to trigger `.github/workflows/release.yml`:

```bash
git tag v0.1.1
git push origin v0.1.1
```

CI validates that the tag matches `APP_VERSION` (the build fails otherwise), pulls the previous release with `vpk download github` to generate delta packages, builds, and publishes the Release via `vpk upload github --publish`. Note that `release-notes.md` must be **committed before tagging** (CI can only read pushed content).

If the CI build fails: fix, commit & push, then delete the old tag and re-create it (the tag must point to the new commit containing the fix; if a Release was half-created before the failure, delete it on the GitHub web UI first):
For example：
```bash
git tag -d v0.1.1                 # delete the local tag
git push origin :refs/tags/v0.1.1 # delete the remote tag
git tag v0.1.1                    # re-create on the current (fixed) commit
git push origin v0.1.1            # re-trigger CI
```

**Option 2: local build** — from the project root, in a terminal with the build venv activated:

```powershell
# 1. (optional) pull the previous release into the feed dir so vpk pack can generate deltas
vpk download github --repoUrl https://github.com/yangjinduo/E-SubDub --outputDir build\dist\update-feed

# 2. Full build
.\build\build_windows.ps1

# 3. Publish to GitHub Releases (needs a write-enabled token; or upload manually via the web UI)
$env:GITHUB_TOKEN = '<your GitHub token>'
vpk upload github --repoUrl https://github.com/yangjinduo/E-SubDub --publish --tag v0.1.1 --outputDir build\dist\update-feed
```

For manual web upload, upload **all files** inside `build\dist\update-feed\` (`releases.win.json` + full/delta nupkg + `yangjinduo.E-SubDub-win-Setup.exe`) to the Release of the corresponding tag, overwriting same-named files.

Once published, installed users receive incremental upgrades via the automatic background check at startup (or the manual "Check for Updates" in the settings dialog). Installed layout: the install root contains `current\` (the app, replaced wholesale on update) and `Update.exe` (the updater), while data directories such as `models\` sit beside `current\` and survive updates.

## Pipeline Stages

Each task writes its output stage by stage, in pipeline order, under its working directory (default `output/`):

| #  | Stage | Directory | Description |
| -- | ----- | --------- | ----------- |
| 01 | Audio extraction | `01_音频提取` | FFmpeg extracts the full audio track |
| 02 | Vocal separation | `02_人声分离` | Demucs separates vocals / accompaniment |
| 03 | Speech recognition | `03_语音识别` | Speaker diarization + sentence-level transcription |
| 04 | Text review | `04_文本审核` | Manual proofreading of transcribed text (skippable) |
| 05 | Translation | `05_翻译` | Index-Translate-2B translation (Homura duration correction enabled for Chinese source) |
| 06 | Translation review | `06_译文审核` | Manual editing of translations (skippable) |
| 07 | Dubbing synthesis | `07_配音生成` | Reference voice picking + IndexTTS-2.5 voice-clone synthesis |
| 08 | Dubbing review | `08_配音复核` | Per-sentence audition / re-generation (skippable) |
| 09 | Mixing | `09_混音` | Dubbing mixed with background audio |
| 10 | Packaging | `10_封装输出` | Final output: MKV packaging (audio track named `AI dubbing {language}`) + bilingual SRT; SRT copy for subtitle-only tasks |

**Generate Subtitles** tasks run only stages 01 / 03 / 04 / 05 / 06 / 10 (no vocal separation or dubbing stages; the final SRT is archived to `10_封装输出`).

**Text-to-Speech** tasks use a separate four-stage pipeline (same working-directory naming):

| #  | Stage | Directory | Description |
| -- | ----- | --------- | ----------- |
| 01 | Text processing | `01_文本处理` | Sentence splitting, language detection (langid), editing (emotion) results written to disk |
| 02 | Speech synthesis | `02_语音合成` | IndexTTS-2.5 clones a voice from the reference audio and synthesizes sentence by sentence (with speed and emotion vectors) |
| 03 | Speech review | `03_语音复核` | Audition / play continuously, delete sentences, change emotions from the emotion column with instant re-generation (skippable) |
| 04 | Packaging | `04_封装输出` | Concatenates inter-sentence silence, writes the WAV / MP3 / M4A / FLAC output named after the task, and generates SRT from actual durations |

## Code Architecture

```
app/
├── main.py                  # Application entry: QApplication, global styles, window icon
├── core/                    # Core logic (UI-independent)
│   ├── config.py            # Global config: app name / version / directory paths / LOGO
│   ├── i18n.py              # Lightweight bilingual dictionary + tr() + language preference persistence
│   ├── hardware.py          # Inference device detection (CUDA / MPS / CPU)
│   ├── ffmpeg.py            # Bundled FFmpeg path resolution (per platform)
│   ├── segment.py           # Segment data structures (sentences / speakers / timeline)
│   ├── workspace.py         # Working directory management: stage states in manifest, checkpoint resume
│   ├── runner.py            # Pipeline engine: stage scheduling, skip completed, invalidate downstream
│   ├── tts_runner.py        # Text-to-speech pipeline engine (split → synthesize → speech review → packaging)
│   ├── langid_util.py       # Automatic text language identification (langid, graceful degradation if missing)
│   ├── task_store.py        # Task list persistence: atomic writes to tasks.json / restore on launch
│   └── task_manager.py      # Task queue: thread lifecycle, task window management
├── pipeline/                # Pipeline stage implementations
│   ├── audio_extract.py     # 01 audio extraction
│   ├── separate.py          # 02 vocal separation (Demucs)
│   ├── asr.py               # 03 speech recognition (MOSS-Transcribe-Diarize + VAD chunking)
│   ├── ml_client.py         # ML inference engine: demucs / ASR worker subprocess management
│   ├── ml_worker.py         # ML subprocess worker (runs in .venv-ml when frozen)
│   ├── translate.py         # 05 translation (including the second Homura duration-correction pass)
│   ├── subtitle.py          # SRT subtitle generation (internal stage)
│   ├── voice_pick.py        # 07 reference voice picking
│   ├── tts.py               # 07 dubbing synthesis (IndexTTS-2.5, separate-venv subprocess)
│   ├── tts_worker.py        # TTS subprocess worker
│   ├── mix.py               # 09 mixing
│   ├── mux.py               # 10 MKV packaging
│   ├── llm_engine.py        # llama.cpp llama-server process management (translation inference service)
│   └── base.py              # Stage common base / utilities
├── resources/               # Resource management
│   ├── model.py             # Registration / status detection / deletion of the six resources
│   ├── downloader.py        # ModelScope model downloader
│   ├── env_setup.py         # Runtime environment setup (.venv-ml, .venv-tts, llama.cpp download)
│   ├── deploy_log.py        # Deployment log: full record of environment setup (deploy-DATE.log)
│   └── logo/                # App LOGO (.ico / .icns)
└── ui/                      # PySide6 interface
    ├── main_window.py       # Main window: resource status bar + task list (three task entries) + system monitor
    ├── style.py             # Global QSS styles
    ├── widgets/             # Task list, resource status bar, import area, task settings, TTS task settings,
    │                        #   segment table, audio player, pipeline panel, subtitle panel, system monitor
    └── dialogs/             # Three task windows, settings, text/translation/dubbing/TTS speech review,
                             #   voice management, TTS editing, stage result viewers
```

**Key design decisions**:

- **Stage-based disk output + manifest checkpoint resume**: Each stage writes to a fixed subfolder with a completion marker; re-running a task on the same file skips completed stages, and manual edits explicitly invalidate downstream stages via the runner.
- **Task list persistence**: tasks.json is saved atomically in the user config directory (Windows `%LOCALAPPDATA%\ESubDub\`, macOS `~/Library/Application Support/ESubDub/`), shared by the task list and global settings (UI language, voice-clone authorization); the task list is restored on launch, and tasks that were running / queued in the previous session are marked "interrupted" and await manual continuation.
- **Isolated inference environments (dual virtual environments)**: The transformers versions required by IndexTTS-2.5 and MOSS-Transcribe-Diarize (4.x / 5.x) are incompatible and cannot share one environment: TTS runs in a dedicated `.venv-tts` subprocess (communicating via `tts_worker`, with torch / numpy shared from the main environment or `.venv-ml` through .pth files); in the installer build the main program has no Python runtime, so ML inference (vocal separation / speech recognition) runs in another `.venv-ml` subprocess (when running from source, the main environment is reused directly). The TTS model is reused across the "generate / review" stages and released once review finishes.
- **Externalized LLM inference**: Translation is inferred via llama.cpp's llama-server (HTTP); the service process is terminated immediately when a task is cancelled.
- **Thread-safe shutdown**: References are released only after task threads confirm exit, avoiding crashes from premature QThread destruction.

## Model Inventory

| Function | Model | Source (ModelScope) |
| -------- | ----- | ------------------- |
| Vocal separation | Demucs (htdemucs) | DiffSynth-Studio/Demucs-Repackage |
| Speech recognition | MOSS-Transcribe-Diarize | openmoss/MOSS-Transcribe-Diarize |
| Subtitle translation | Index-Translate-2B | — |
| Duration correction (Chinese source) | Index-Homura-2B (Q8\_0 GGUF) | Abu-Dju/Index-Homura-2B-Q8\_0-GGUF |
| Dubbing synthesis | IndexTTS-2.5 | IndexTeam/IndexTTS-2.5 |

## Folder Classification

| Category | Directory | Description |
| -------- | --------- | ----------- |
| Required at runtime | `app/` | All source code |
| Required at runtime | `libs/` | Bundled FFmpeg (windows/ffmpeg.exe, macos/Arm + Intel) |
| Required at runtime | `tools/llama.cpp/` | llama-server inference service + CUDA DLLs (auto-downloaded by the app) |
| Required at runtime | `third_party/index-tts/` | IndexTTS engine source code |
| Required at runtime | `torchaudio/` | Shim package matching torch 2.14 (demucs dependency — do not pip-install over it) |
| Required at runtime | `.venv-tts/` | Dedicated TTS virtual environment (auto-rebuilt by the app) |
| Required at runtime | `.venv-ml/` | ML inference virtual environment (used by the installer build, auto-rebuilt by the app) |
| Required at runtime | `models/` | The five models (auto-downloaded by the app, gitignored) |
| Generated at runtime | `output/` | Task working directories and final outputs (gitignored) |
| Development | `build\build_windows.ps1` | Windows one-click build script (Nuitka freeze + Inno Setup, see "Building the Installer") |
| Development | `build\installer.iss` | Inno Setup installer configuration (per-user install / space check / partition selection) |
| Development | `build\requirements-freeze.txt` | Minimal dependency list for the frozen build (no heavy inference packages) |
| Development | `build\runtime\` | Portable CPython staging (manually downloaded and extracted, distributed with the installer) |
| Development | `build\dist\`, `build\work\` | Build artifacts and Nuitka intermediate directories (gitignored) |
| Development | `venv/` | Main development virtual environment |
| Development | `requirements.txt`, `.gitignore`, `安装引导打包阶段注意事项.TXT` | Dependency list / packaging-phase notes |
| Testing | `test/` | e2e smoke tests, TTS probe scripts + test videos (gitignored) |

## Voice Cloning Notice

The voice cloning features of this software (video dubbing and text-to-speech) may only be used when you hold the legal right to use the voice in the original audio, and the generated dubbing and speech may only be used for lawful purposes. An authorization confirmation dialog is shown on first use.

## Acknowledgements

This project is built on many outstanding open-source models, tools, and frameworks. Our sincere thanks to all open-source developers and communities!

### Open-Source Model Acknowledgements

- **[Demucs (htdemucs)](https://github.com/adefossez/demucs)**: Thanks to Meta and the community maintainers for open-sourcing this high-precision vocal separation model, which underpins this project's core audio vocal/accompaniment separation capability.

- **[MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize)**: Thanks to the openmoss team for open-sourcing this speaker-diarized speech recognition model, enabling accurate audio transcription and sentence-level speaker identification.

- **Index-series models**: Thanks to IndexTeam, Abu-Dju, and other developers for open-sourcing [Index-Translate-2B](https://github.com/bilibili/Index-Translate), [Index-Homura-2B](https://modelscope.cn/models/Abu-Dju/Index-Homura-2B-Q8_0-GGUF), and [IndexTTS-2.5](https://github.com/IndexTeam/IndexTTS), providing the core technology behind this project's intelligent subtitle translation, Chinese duration alignment, and high-quality voice-cloned dubbing.

### Open-Source Tools & Engine Acknowledgements

- **[llama.cpp](https://github.com/ggml-org/llama.cpp)**: Thanks to the llama.cpp project for its lightweight, high-performance local LLM inference service, powering efficient local inference for the translation model.

- **[FFmpeg](https://github.com/FFmpeg/FFmpeg)**: Thanks to the FFmpeg open-source multimedia framework for full-pipeline multimedia processing — audio extraction, video packaging, and audio/video mixing.

### Development Framework & Community Acknowledgements

- **[PySide6](https://wiki.qt.io/Qt_for_Python)**: Thanks to the PySide6 open-source GUI framework for powering this project's cross-platform desktop client interface.

- **[PyTorch](https://github.com/pytorch/pytorch) / [Torchaudio](https://github.com/pytorch/audio)**: Thanks to the PyTorch deep learning framework and the Torchaudio audio processing library for the computational and technical foundation of model inference and audio preprocessing.

- **[ModelScope](https://github.com/modelscope/modelscope)**: Thanks to ModelScope for model hosting and download acceleration, giving users convenient automatic model deployment.

- **[langid](https://github.com/saffsd/langid)**: Thanks to the langid open-source text language detection tool for automatic multi-language text identification.

Our thanks also go to all developers and contributors of the open-source ecosystem — its flourishing growth made this project possible.

### Open Source Notices

All open-source software, models, and licenses used, distributed with the package, or deployed at runtime are collected in [THIRD-PARTY-NOTICES-EN.html](THIRD-PARTY-NOTICES-EN.html) (Chinese version: [THIRD-PARTY-NOTICES-CN.html](THIRD-PARTY-NOTICES-CN.html); distributed with the installer, and shown by the in-app footer "Open Source Notice" entry according to the UI language). The notice fulfills the accompanying obligations of Apache-2.0 / MIT style licenses and indicates how to obtain the corresponding PySide6 (LGPLv3) source code. When upgrading dependencies, update this file accordingly: the frozen main-program list is in `build\requirements-freeze.txt`, and the sub-environment lists are in `app\resources\env_setup.py`.

## Known Limitations

The hard-subtitle removal feature is not yet implemented. It could rely on video restoration models such as ProPainter and STTN to intelligently remove burned-in subtitles and restore a clean video, completing the full video localization and dubbing/subtitle production pipeline.

Due to limited GPU memory on my hardware, I have not been able to complete the development and testing of this feature. Contributions from developers and enthusiasts are warmly welcomed — feel free to open PRs and Issues to help iterate and improve the project.

## License

Apache 2.0
