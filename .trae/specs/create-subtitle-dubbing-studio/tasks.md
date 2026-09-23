# Tasks

## Phase 0: 技术验证（POC，最高优先级）

- [x] Task 1: 三大模型 + Demucs 本地推理 POC
  - [x] SubTask 1.1: MOSS-Transcribe-Diarize 在 Windows/macOS 的本地推理验证（transformers 路径，CUDA/MPS/CPU 三种设备），确认显存/内存需求与实际磁盘占用，验证说话人标签+时间戳 JSON 输出
  - [x] SubTask 1.2: IndexTTS-2.5 本地推理验证：零样本克隆调用方式、原生时长控制 API 用法与精度、5 语种支持确认、磁盘占用【结论：克隆可行；时长控制不可用→time-stretch 方案；磁盘 10.22GB；CPU RTF 14-33 极慢需 GPU；transformers 4.52 依赖→独立 venv 子进程】【2026-09-21：因依赖冲突与磁盘占用，TTS 已换型为 MOSS-TTS-Local-1.7B，本 POC 结论仅作历史记录】
  - [x] SubTask 1.3: Qwen3-4B-GGUF + llama.cpp 集成验证（内置引擎、CUDA/Metal 后端、非思考模式、JSON 输出稳定性）；顺带验证 1.7B/8B 档位
  - [x] SubTask 1.4: Demucs htdemucs 集成验证（vocals/no_vocals 分离质量、CPU 模式速度）
  - [ ] SubTask 1.5: 产出 POC 报告：各模型确切磁盘占用（修正 UI 显示值）、显存基线、长音频分段+跨段说话人标签合并方案定型【数据已齐，汇总入 checklist 验证阶段】

## Phase 1: 应用骨架

- [ ] Task 2: 项目初始化与工程配置
  - [ ] SubTask 2.1: PySide6 项目结构搭建（app/ 模块划分：ui/ pipeline/ resources/ core/）+ `.venv` 虚拟环境创建（python -m venv .venv，依赖全部装入虚拟环境）
  - [ ] SubTask 2.2: FFmpeg 调用封装（自动选择 libs/{windows|macos/Arm|macos/Intel} 平台二进制）
  - [ ] SubTask 2.3: PyInstaller 打包配置（Windows/macOS 双平台）
- [x] Task 3: 硬件检测模块
  - [x] SubTask 3.1: 检测 NVIDIA(CUDA)/Apple Silicon(MPS)/Intel Mac(CPU)，暴露统一设备选择接口供各模型使用
  - [x] SubTask 3.2: 设备状态上报 UI（含 Intel Mac "速度较慢"标注）
- [x] Task 4: 资源管理器
  - [x] SubTask 4.1: 5 项资源的状态检测（FFmpeg/Demucs/MOSS-TD/LLM/MOSS-TTS：已就绪/未就绪）
  - [x] SubTask 4.2: 魔搭下载器（应用内下载、进度、断点重试、HF 备选源、下载位置选择）
  - [x] SubTask 4.3: 资源删除与位置管理；LLM 档位切换（4B 默认 / 1.7B / 8B）

## Phase 2: 主界面

- [x] Task 5: 主窗口框架
  - [x] SubTask 5.1: 顶部资源状态栏（功能名+模型名+状态+设置按钮，参考 ui-ux-pro-max skill 设计）
  - [x] SubTask 5.2: 媒体导入区（拖拽+点击选择，视频/音频，文件信息展示）
  - [x] SubTask 5.3: 任务设置区（源语言/目标语言/字幕形式三选一/配音开关默认开/输出目录）
- [x] Task 6: 资源设置对话框（已就绪：路径+删除；未就绪：下载大小+下载按钮+模型主页链接+下载进度）【UI 与信号完成；下载逻辑由 Task 4 下载器接入】
- [x] Task 7: 流程进度界面（步骤级进度条、当前状态、日志、审核暂停点恢复）【步骤链与日志 UI 完成；暂停点恢复已随 Task 22 编排接入（runner 信号 → 面板状态/进度/日志）】

## Phase 3: 流水线后端模块

- [x] Task 8: 音频提取（视频→full_audio.wav；音频输入跳过）
- [x] Task 9: 人声分离（Demucs htdemucs → vocals.wav/no_vocals.wav；仅字幕模式默认跳过+进阶选项）
- [x] Task 10: ASR 转写（MOSS-TD → [{spk,start,end,text}]；>90 分钟分段+跨段说话人标签自动合并）
- [x] Task 11: LLM 引擎与校对模块
  - [x] SubTask 11.1: llama.cpp 进程管理（启动/健康检查/非思考模式）
  - [x] SubTask 11.2: 滑动窗口分批校对（保留时间戳、修复断句、标记删除不物理删除）→ corrected.json
- [x] Task 12: LLM 翻译模块（长度约束按 end-start 时长、滑动窗口）→ translated.json
- [x] Task 13: 说话人参考音色自动挑选（每 spk 干净长句）
- [x] Task 14: TTS 生成模块【2026-09-21 换型调整：IndexTTS-2.5 → MOSS-TTS-Local-1.7B（魔搭 openmoss/MOSS-TTS-Local-Transformer，约 6.1GB），与主环境共用 transformers 5.17（trust_remote_code 加载，torchaudio 音频 IO），不再需要 .venv-tts 隔离环境/子进程；克隆仅需参考音频；时长优先用原生 token 级控制（1s ≈ 12.5 tokens），time-stretch 兜底】【实现调整：音频 IO 用 soundfile（本机 torchaudio≥2.9 无 TorchCodec 后端）】
  - [x] SubTask 14.1: MOSS-TTS-Local-1.7B 加载 POC（transformers 5.17 + trust_remote_code；MPS/CUDA/CPU；audio tokenizer 独立仓库 openmoss/MOSS-Audio-Tokenizer 已预下载到 models/tts/MOSS-Audio-Tokenizer）【M4 实测待真机测试阶段】
  - [x] SubTask 14.2: TTS 引擎模块（进程内加载：processor + AutoModel，克隆调用 build_user_message(text, reference=[wav])，逐句生成 wav + 时长 + 伸缩比）→ app/pipeline/tts.py
  - [x] SubTask 14.3: TTS 调度器（token 级时长控制生成 → 偏差 time-stretch 对齐 → 伸缩比 >1.3x/<0.7x 打 needs_review 标记）→ app/core/runner.py::_run_tts
- [x] Task 15: 混音模块（响度对齐原人声→时间轴排布→叠加 no_vocals → ai_new_audio.wav）
- [x] Task 16: 字幕生成（单语 / 双语目标在上 / 双语源在上 → SRT）
- [x] Task 17: MKV 封装（追加 AAC 音轨设默认+软字幕轨带语言标记，视频 copy，原音轨保留）

## Phase 4: 审核界面

- [x] Task 18: ASR 审核界面（表格：时间/说话人下拉/文本编辑/片段试听；标记删除置灰可恢复；说话人修改联动 TTS 音色）
- [x] Task 19: 译文审核界面（原文/译文对照、可编辑、跳过审核开关）【跳过审核开关在 Task 22 编排接入】
- [x] Task 20: 说话人音色管理界面（自动挑选结果展示、试听、手动更换参考片段）
- [x] Task 21: TTS 异常复听界面（超限句列表、逐条试听、改译文重新生成）

## Phase 5: 编排与分发

- [x] Task 22: 流水线编排整合【2026-09-21 完成：app/core/runner.py（QThread 编排 + 审核暂停点 + 设备回退 + 进度权重）+ app/core/workspace.py（manifest 阶段复用）+ main_window 接入（开始/停止按钮、四类审核对话框分发、清理询问弹窗、closeEvent 线程保护）】
  - [x] SubTask 22.1: 配音模式/仅字幕模式端到端串联（含审核暂停点）
  - [x] SubTask 22.2: 中间产物管理（同名文件夹、manifest 阶段状态、重复执行跳过复用）
  - [x] SubTask 22.3: 完成后临时文件清理询问弹窗（删除保留成品 / 留作备用）
- [ ] Task 23: 分发打包（Win/macOS 安装包、首次配音使用的声音授权提示、模型引导下载首启流程）【声音授权提示已在 main_window 实现（get_setting("voice_clone_ack")），其余待做】

# Task Dependencies
- Task 1（POC）blocks Task 9/10/11/12/13/14 —— 模型集成前必须完成
- Task 2 → Task 3、4、5
- Task 5 → Task 6、7
- Task 4 → Task 8-17（流水线依赖资源管理）
- Task 10+11 → Task 18；Task 12 → Task 19；Task 13 → Task 20；Task 14 → Task 21
- Task 8-21 → Task 22 → Task 23
- 可并行：Task 3/4 与 Task 5/6/7；Phase 3 各后端模块间在 POC 完成后可并行
