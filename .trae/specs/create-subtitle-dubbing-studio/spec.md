# 极简字幕与配音生成器（Subtitle & Dubbing Studio）Spec

## Why
为外语视频/音频自动生成目标语言字幕，并可选地通过"人声分离 + 声音克隆 TTS"生成本地化配音音轨，封装为保留原音轨的 MKV。全流程使用开源模型本地推理，面向非专业用户的桌面软件，需支持 Windows 与 macOS 分发。

## What Changes
- 全新 PySide6 桌面应用：资源状态栏、媒体导入、任务设置、流程进度、两个承诺人工审核界面（ASR 审核、译文审核）、说话人音色管理、TTS 异常复听
- 开源模型链集成（全部本地推理，自动检测 CUDA/MPS/CPU）：
  - FFmpeg（已内置 `libs/windows/ffmpeg.exe`、`libs/macos/Arm/ffmpeg`、`libs/macos/Intel/ffmpeg`）— 音频提取 / 混音 / MKV 封装
  - Demucs htdemucs（约 80MB，POC 实测）— 人声分离
  - MOSS-Transcribe-Diarize（约 1.7GB，POC 实测）— 说话人分离 ASR
  - Qwen3-4B-GGUF（Q4_K_M 约 2.5GB，llama.cpp 内置引擎；低配 1.7B / 高配 8B 可选档）— 校对与翻译
  - MOSS-TTS-Local-1.7B（魔搭 `openmoss/MOSS-TTS-Local-Transformer`，约 6.1GB；audio tokenizer 为独立仓库另行下载）— 零样本声音克隆（仅需参考音频，无需转写文本）；原生 token 级时长控制（1s ≈ 12.5 tokens，精度待 POC），兜底 time-stretch
- TTS 环境方案（2026-09-21 更新，取代原 IndexTTS-2.5 + .venv-tts 隔离方案）：MOSS-TTS 与主流水线共用 transformers 5.17（官方推荐 5.0.0，源码核验兼容），trust_remote_code 加载，**不再需要独立 venv / 子进程**
- 输出：SRT 字幕文件 + MKV（追加新配音音轨设为默认音轨 + 软字幕轨，视频流不重编码，原音轨保留）
- 两种模式：配音模式（完整流水线）/ 仅字幕模式（跳过人声分离、TTS、混音、封装）
- 中间产物按"导入文件名"建文件夹存储、可复用（重复执行自动跳过已完成阶段），完成后询问清理

## Impact
- Affected specs: 无（绿地项目）
- Affected code: 新建整个应用；复用 `libs/` 下三平台 FFmpeg 二进制
- 关键风险：MOSS-Transcribe-Diarize 官方主推 SGLang 部署，Windows/macOS 桌面端 transformers 本地推理路径需 POC 首先验证（Task 1，最高优先级）

## 已确认的设计决策
| 决策点 | 结论 |
|---|---|
| UI 技术栈 | PySide6，PyInstaller 打包 Win/macOS |
| 字幕封装 | 软字幕轨封装进 MKV，不烧录、不重编码视频 |
| 译文审核 | 翻译完成 → 译文人工审核 → 批量 TTS（提供"跳过审核"开关） |
| 配音目标语言 | 首发中文/英文为主，架构预留 MOSS-TTS 20 语种扩展；仅字幕模式目标语言不受此限 |
| 人声分离 | Demucs htdemucs 为默认 |
| 参考音色 | 自动挑选 + 审核界面可试听/手动更换 |
| 硬件 | NVIDIA→CUDA；Apple Silicon→MPS/Metal；Intel Mac→CPU（支持但 UI 标注"速度慢"） |
| 中间产物 | 输出目录下按导入文件名建文件夹；manifest 记录阶段完成状态，重复执行跳过已完成阶段；完成后弹窗询问"删除临时文件 / 留作备用" |
| 默认音轨 | 新配音轨封装后设为默认音轨，原音轨保留但取消默认标记 |
| 模型下载源 | 魔搭社区为主（国内快），HuggingFace 备选 |
| 软件定位 | 需分发他人：安装包 + 首次使用声音克隆授权提示 |
| 说话人联动 | 审核界面修改某句说话人后，该句 TTS 参考音色自动跟随新说话人 |
| LLM 删除 | LLM 判定的"无用段落"只做标记删除（置灰可恢复），不物理删除 |
| Python 环境 | 项目使用 `.venv` 虚拟环境（`python -m venv .venv`），所有依赖安装于虚拟环境内，不污染系统 Python |

## ADDED Requirements

### Requirement: 平台与运行环境
系统 SHALL 在 Windows 10+（x64）与 macOS（Apple Silicon / Intel）运行；单进程 PySide6 桌面应用；FFmpeg 使用 `libs/` 内置对应平台二进制。

#### Scenario: 跨平台启动
- **WHEN** 用户在 Windows 或 macOS 上启动应用
- **THEN** 自动选用对应平台的 FFmpeg 与推理后端，界面语言为中文

### Requirement: 硬件加速自动检测
系统 SHALL 在启动时检测 GPU：NVIDIA 显卡存在→启用 CUDA；Apple Silicon→启用 MPS/Metal；Intel Mac 或无独显→CPU 模式并在 UI 标注"速度较慢"。所有模型（Demucs/MOSS-TD/MOSS-TTS/llama.cpp）按检测结果自动选择设备。UI 应显示当前推理设备。

#### Scenario: 检测到 NVIDIA
- **WHEN** Windows 机器存在可用 NVIDIA 显卡（torch.cuda 可用）
- **THEN** 全部模型以 CUDA 运行，资源栏显示"CUDA 已加速"

#### Scenario: Intel Mac
- **WHEN** 应用运行于 Intel Mac
- **THEN** 全部模型以 CPU 运行，UI 显著标注"当前为 CPU 模式，处理速度较慢"

### Requirement: 资源状态栏
主窗口顶部 SHALL 横向显示 5 个资源卡片：①音频提取(FFmpeg) ②人声分离(Demucs htdemucs) ③音频转文字(MOSS-Transcribe-Diarize) ④文本校对与翻译(Qwen3-4B-GGUF) ⑤声音克隆(MOSS-TTS-Local-1.7B)。每卡片：上方为功能名（如"音频转文字"），下方为对应模型名；状态为"已就绪"/"未就绪"。所有卡片右侧 SHALL 放置一个统一的"设置"按钮（不再在每张卡片上单独放置），点击打开统一设置窗口：集中查看全部资源所在目录及准备状态、执行删除与下载、切换界面语言（i18n，中/英，即时生效并持久化）。

#### Scenario: 全部就绪
- **WHEN** 5 项资源均已可用
- **THEN** 状态栏全部显示"已就绪"，任务可启动

### Requirement: 资源设置与下载管理
点击资源卡片设置按钮 SHALL 弹出对话框：
- 已就绪：显示资源文件位置 + 删除按钮（删除后状态回到未就绪）
- 未就绪：显示"需下载 XX，约占用磁盘 XX GB" + 应用内下载按钮 + "模型主页"链接（魔搭/HuggingFace）；点击下载→弹窗选择下载位置→开始下载并显示进度条

下载 SHALL 支持断点显示与失败重试；模型默认从魔搭社区下载，HuggingFace 为备选源。LLM 资源 SHALL 支持档位切换（默认 Qwen3-4B-GGUF Q4_K_M，可选 1.7B/8B）。

#### Scenario: 首次使用下载模型
- **WHEN** 用户点击未就绪资源的下载按钮并选择位置
- **THEN** 开始下载，卡片/对话框显示进度百分比，完成后状态变为"已就绪"

### Requirement: 媒体导入
主窗口 SHALL 提供大区域支持拖拽或点击文件选择窗口导入视频（MKV/MP4 等常见格式）或音频（WAV/MP3 等）。导入后显示文件名、时长、媒体类型。

#### Scenario: 拖入视频
- **WHEN** 用户拖入 MKV 视频文件
- **THEN** 显示文件信息，任务可配置可启动

#### Scenario: 导入纯音频
- **WHEN** 用户导入音频文件
- **THEN** 跳过音频提取步骤，该音频直接进入后续流程

### Requirement: 任务设置
主窗口 SHALL 提供：
- 字幕设置：源语言（自动检测默认）、目标语言、字幕形式三选一（单语字幕 / 双语字幕-目标语言在上 / 双语字幕-源语言在上）
- 配音开关（默认开启）
- 输出目录设置

#### Scenario: 仅生成字幕
- **WHEN** 用户关闭配音开关并启动任务
- **THEN** 执行仅字幕模式流水线，输出 SRT 文件

### Requirement: 中间产物管理
系统 SHALL 在输出目录下创建与导入文件同名的文件夹存储全部中间产物（full_audio.wav、vocals.wav、no_vocals.wav、asr.json、corrected.json、translated.json、tts/ 分段音频、ai_new_audio.wav 等）与成品。文件夹内 manifest.json 记录各阶段完成状态；重复执行同文件任务时，检测到已完成的阶段 SHALL 跳过并复用产物。

#### Scenario: 复用中间产物
- **WHEN** 用户对同一文件再次启动任务且此前已完成人声分离
- **THEN** 跳过分离步骤直接复用 vocals.wav/no_vocals.wav

#### Scenario: 完成清理询问
- **WHEN** 任务全部完成
- **THEN** 弹窗提示存在临时文件，用户可选"删除临时文件（保留成品）"或"留作备用"

### Requirement: 流水线-音频提取
配音模式/仅字幕模式下，若导入为视频，系统 SHALL 用 FFmpeg 提取音频为 full_audio.wav；导入为音频则跳过。

### Requirement: 流水线-人声分离
系统 SHALL 用 Demucs htdemucs 将 full_audio.wav 分离为 vocals.wav（对白人声）与 no_vocals.wav（背景音）。仅字幕模式默认跳过分离（ASR 直接处理 full_audio.wav），提供"先分离再识别（更准但更慢）"进阶选项。

### Requirement: 流水线-ASR 转写
系统 SHALL 用 MOSS-Transcribe-Diarize 处理人声音频，输出 JSON：`[{spk, start, end, text}]`。超过约 90 分钟的音频 SHALL 分段转写（带重叠区）并自动合并跨段说话人标签（按声纹/上下文对齐，POC 验证方案）。

#### Scenario: 长电影转写
- **WHEN** 输入 2 小时音频
- **THEN** 自动分段转写后合并为统一说话人标签（S01/S02…全局一致）的单一 JSON

### Requirement: 说话人参考音色挑选
系统 SHALL 为每个说话人自动挑选"干净长句"作为 TTS 音色参考片段，并在审核界面展示，支持试听与手动更换参考片段。

### Requirement: LLM 校对
系统 SHALL 用本地 LLM（llama.cpp 引擎，非思考模式）校对 ASR 文本：修正识别错误、合并/修复不完整断句、整段无用文本仅做标记删除（不物理删除）。**必须保留原有 start/end 时间戳**。长文本 SHALL 滑动窗口分批处理（如每批 50 条、重叠 5 条）。输出同结构 corrected.json。

#### Scenario: 标记删除可恢复
- **WHEN** LLM 判定某段为无意义噪声文本
- **THEN** 该段在审核界面置灰显示并可一键恢复，而非直接删除

### Requirement: ASR 人工审核
LLM 校对后系统 SHALL 暂停流水线，弹出 ASR 审核界面：表格列出时间/说话人/文本；文本可编辑；说话人通过下拉菜单修改；每句可试听对应音频片段；标记删除的句子置灰可恢复。确认后流程继续。修改说话人 SHALL 联动该句 TTS 参考音色（后续 TTS 按新说话人克隆）。

#### Scenario: 修正说话人归属
- **WHEN** 用户将某句说话人从 S01 改为 S02 并确认
- **THEN** 该句后续 TTS 使用 S02 的参考音色克隆

### Requirement: LLM 翻译
系统 SHALL 用本地 LLM 将校对后文本翻译为目标语言，输出同结构 translated.json：`[{spk, start, end, target_text}]`，翻译时按 `end-start` 时长施加译文长度约束（适配目标语速，超长压缩改写）。保留时间戳与说话人标签。滑动窗口分批处理。

### Requirement: 译文人工审核
翻译完成后、批量 TTS 前，系统 SHALL 暂停并弹出译文审核界面（原文/译文对照、可编辑译文、可跳过审核开关）。确认后进入 TTS。

### Requirement: TTS 生成与时长对齐
系统 SHALL 按说话人参考音色用 MOSS-TTS-Local-1.7B 逐句生成目标语言语音（仅需参考音频，无需参考文本转写）：
- 优先使用原生 token 级时长控制（目标 tokens ≈ (end-start) × 12.5）按目标时长生成
- 生成后时长仍偏差时用 time-stretch（不变调）拉伸/填充静音对齐
- 伸缩比 >1.3x 或 <0.7x 的句子 SHALL 标记，进入异常复听界面

#### Scenario: 时长超标
- **WHEN** 某句生成音频需 >1.3x 拉伸才能对齐
- **THEN** 该句标记为待复听，用户可试听、修改译文后重新生成

### Requirement: TTS 异常复听
系统 SHALL 提供异常复听界面：列出所有伸缩超限句子，逐条试听，支持改译文重新生成该句。

### Requirement: 混音
系统 SHALL：
1. 测量原 vocals.wav 综合响度，将全部 TTS 片段响度对齐到该值（loudnorm/增益）
2. 按时间轴排布 TTS 片段
3. 与 no_vocals.wav 叠加混音 → ai_new_audio.wav

#### Scenario: 响度自然
- **WHEN** 混音完成
- **THEN** 新配音与背景音的响度比例与原片人声/背景比例一致

### Requirement: 字幕生成
系统 SHALL 按用户所选字幕形式生成 SRT：
- 单语字幕：目标语言
- 双语字幕（目标语言在上）：上行目标语言、下行源语言
- 双语字幕（源语言在上）：上行源语言、下行目标语言

### Requirement: MKV 封装
系统 SHALL 用 FFmpeg 将 ai_new_audio.wav（编码 AAC）追加为独立音轨、SRT 追加为软字幕轨封装进原 MKV：视频流 `-c copy` 不重编码、原音轨全部保留、新配音轨设为默认音轨、字幕轨带语言标记。成品命名清晰（如 `原名.dubbed.mkv`）。

#### Scenario: 封装结果
- **WHEN** 封装完成
- **THEN** 播放器默认播放新配音轨，可手动切回原音轨、开关字幕轨，视频画质无损

### Requirement: 仅字幕模式
配音关闭时系统 SHALL 执行简化流水线：提取音频（音频输入跳过）→（可选人声分离）→ ASR → LLM 校对 → ASR 人工审核 → LLM 翻译 → 译文审核（可跳过）→ 生成 SRT 文件；跳过 TTS/混音/MKV 封装。注：因字幕形式均含目标语言，翻译步骤保留；若源语言=目标语言则跳过翻译。

### Requirement: 流程进度展示
主窗口 SHALL 提供流程进度区：当前步骤、步骤级进度条（如"ASR 转写中 45%"、TTS 逐句计数）、运行日志。耗时步骤显示预估信息。流水线可在审核暂停点继续。

### Requirement: 分发与授权
软件 SHALL 可分发：PyInstaller 打包 Windows 安装包与 macOS 应用；首次使用配音功能时展示"声音克隆使用授权"提示（用户确认拥有原声使用权利）。

## 待 POC 验证项（Task 1 产出）
1. MOSS-Transcribe-Diarize 在 Win/macOS 的 transformers 本地推理路径（CUDA/MPS/CPU）及实际显存需求
2. MOSS-TTS-Local-1.7B 在 transformers 5.17 下的加载与推理（trust_remote_code + torchaudio，CUDA/MPS/CPU）；token 级时长控制（1s ≈ 12.5 tokens）的精度；audio tokenizer 独立仓库（OpenMOSS-Team/MOSS-Audio-Tokenizer）的预下载集成
3. 各模型确切磁盘占用（修正 UI 显示的"约 XX GB"）
4. Demucs htdemucs 在无 GPU 环境的可行速度
