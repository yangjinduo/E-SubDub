"""轻量 i18n 模块：中英双语字典 + tr() 取词 + 语言偏好持久化。

设计说明：
- 不使用 Qt Linguist（.ts/.qm）体系，改用 Python 字典，便于在 PyInstaller
  打包中零成本内置，且流水线等非 UI 模块也能直接调用 tr()；
- 语言偏好保存到用户配置目录（platformdirs），路径：
  <user_config_dir>/MinimSubDub/settings.json；
- tr() 查找顺序：当前语言 → zh-CN 兜底 → 返回 key 本身（便于发现漏译）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from platformdirs import user_config_dir

logger = logging.getLogger(__name__)

APP_NAME = "MinimSubDub"  # 配置目录标识（无空格）；显示名见 AppConfig.APP_NAME
DEFAULT_LANGUAGE = "zh-CN"

# 支持的语言：代码 → 原生名称（原生名称不翻译，始终以其语言本身显示）
SUPPORTED_LANGUAGES: dict[str, str] = {
    "zh-CN": "简体中文",
    "en": "English",
}

# ---------------------------------------------------------------- 翻译字典

TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh-CN": {
        # ---- 通用 ----
        "common.ready": "已就绪",
        "common.not_ready": "未就绪",
        "common.close": "关闭",
        "common.cancel": "取消",
        "common.confirm": "确认",
        "common.settings": "设置",
        "common.delete": "删除",
        "common.download": "开始下载",
        "common.model_homepage": "模型主页",
        # ---- 主窗口 ----
        "main.title": "Minim SubDub",
        "main.start": "开始处理",
        "main.start_batch": "开始批量处理",
        "main.log.batch_started": "开始批量处理：共 {count} 个文件",
        "main.log.batch_queued": "其余 {count} 个文件已加入任务队列",
        "main.log.init": "初始化完成：{n}/{total} 项资源就绪（FFmpeg 为内置工具，其余模型待下载）。",
        "main.log.device_initial": "初始设备检测：{desc}",
        "main.log.device_done": "设备检测完成：{desc}",
        "main.log.imported": "已导入文件：{name}",
        "main.log.no_file": "请先导入媒体文件。",
        "main.log.task_settings": "源语言 {src} → 目标语言 {dst}",
        "main.log.missing_resources": "以下资源未就绪，请先在设置中下载：{names}",
        "main.log.llm_server_missing": "未找到 llama-server（macOS 可通过 brew install llama.cpp 安装），无法执行字幕翻译。",
        "main.log.finished": "处理完成：{outputs}",
        "main.log.cancelled": "已取消处理（已完成阶段的产物已保留，重新开始可继续）。",
        "main.log.failed": "处理失败：{error}",
        "main.stop": "停止处理",
        "main.log.cancelling": "正在取消，等待当前步骤结束…",
        "main.close_confirm_title": "确认关闭",
        "main.close_confirm_text": "有任务正在执行，确认中断任务并关闭窗口吗？",
        "main.cleanup_title": "处理完成",
        "main.cleanup_question": "任务已完成。是否删除中间临时文件？\n成品将保留：\n{files}",
        "main.cleanup_delete": "删除临时文件",
        "main.cleanup_keep": "留作备用",
        "main.cleanup_done": "临时文件已清理，成品保留在工作目录。",
        "main.voice_clone_title": "声音克隆使用授权",
        "main.voice_clone_text": (
            "您即将使用声音克隆功能生成配音。请确认您拥有原音频中声音的合法使用权利，"
            "且生成的配音仅用于合法用途。"
        ),
        "main.voice_clone_agree": "我已确认拥有授权",
        # ---- 流程引擎 ----
        "runner.started": "开始处理：{name}",
        "runner.stage_skipped": "跳过{stage}（复用已有产物）",
        "runner.lang_detected": "检测到源语言：{lang}",
        "runner.homura_fix": "译文时长修正：{overlong} 句预计超长，{fixed} 句已压缩重译（其余由配音伸缩兜底）",
        "runner.device_fallback": "{stage}加速推理失败（{error}），回退 CPU 重试",
        "runner.srt_done": "字幕已生成：{path}（{count} 条）",
        "runner.mux_skipped_audio_input": "输入为音频文件，跳过 MKV 封装（混音 wav 与字幕已归档至封装输出）",
        "runner.voice_picked": "已为 {count} 位说话人挑选参考音色",
        "runner.voice_reused": "复用已确认的参考音色",
        "runner.voice_none": "无法挑选参考音色（缺少足够长的有效语音段），请检查人声分离结果",
        "runner.seg_ref_ready": "逐句切片完成：{own} 句以自身切片作参考（保留原句情绪），{fallback} 句回退说话人参考",
        "runner.tts_no_ref": "说话人 {spk} 的句子无可用参考音频，跳过该句配音",
        "runner.tts_progress": "配音生成 {done}/{total} 句",
        "runner.tts_progress_pct": "配音生成 {done}/{total} 句（{pct}%）",
        "runner.tts_review_needed": "配音复核：共 {count} 句，可试听、修改译文并即时重生成",
        "runner.srt_synced": "配音复核修改了 {count} 句译文，已同步重新生成字幕",
        "runner.mix_overlay": "叠加原声混音：对白时段用新配音，句间保留原声（含语气词）",
        "runner.tts_env_missing": "TTS 运行环境未就绪：{detail}（IndexTTS-2.5 需独立环境 .venv-tts 与源码 third_party/index-tts）",
        "runner.auto_review_passed": "人工审核自动通过（挂机批量模式）",
        # ---- 资源状态栏 ----
        "bar.device_title": "推理设备",
        "bar.device_value": "{value}",
        "bar.env_tts": "TTS 环境",
        "bar.env_llama": "LLAMA 服务",
        "resource.ffmpeg.feature": "音频提取",
        "resource.demucs.feature": "人声分离",
        "resource.transcribe.feature": "音频转文字",
        "resource.translate.feature": "字幕翻译",
        "resource.homura.feature": "配音时长修正",
        "resource.tts.feature": "声音克隆",
        # ---- 运行环境搭建 ----
        "settings.env_feature": "运行环境",
        "settings.env_sub": "TTS 虚拟环境 · LLAMA 服务",
        "settings.env_ready_detail": ".venv-tts 与 llama-server 均已就绪",
        "settings.env_missing_detail": "缺少 TTS 虚拟环境或 llama-server，点击右侧“开始搭建”完成准备",
        "settings.env_setup": "开始搭建",
        "env.setup_started": "开始搭建运行环境（TTS 虚拟环境 + LLAMA 服务）…",
        "env.setup_done": "运行环境搭建完成：{detail}",
        "env.setup_failed": "运行环境搭建失败：{error}",
        "env.setup_cancelled": "已取消环境搭建；已完成的部分下次搭建时自动跳过。",
        "env.step.check_tts": "检查 TTS 运行环境…",
        "env.step.venv": "创建独立虚拟环境 .venv-tts…",
        "env.step.pth": "配置与主环境共享依赖（torch 等）…",
        "env.step.clone": "克隆 IndexTTS 源码（gh-proxy 加速）…",
        "env.step.pip_core": "安装核心依赖（transformers 4.52 / torchaudio 2.8）…",
        "env.step.pip_runtime": "安装 IndexTTS 运行依赖…",
        "env.step.pip_sub": "安装音频处理子依赖（matplotlib / tensorboard 等）…",
        "env.step.strip": "剥离 torchaudio C 扩展（适配共享 torch）…",
        "env.step.verify": "验证依赖导入…",
        "env.step.llama_api": "查询 llama.cpp 最新版本…",
        "env.step.llama_download": "下载 llama-server",
        "env.step.llama_extract": "解压 llama-server…",
        # ---- 推理设备描述 ----
        "device.cuda": "CUDA（{name}）",
        "device.mps": "MPS（Apple Silicon 加速）",
        "device.cpu": "CPU（速度较慢）",
        # ---- 任务设置 ----
        "task.title": "任务设置",
        "task.source_language": "源语言",
        "task.target_language": "目标语言",
        "task.subtitle_mode": "字幕形式",
        "task.overlay_original": "混音后叠加原背景音",
        "task.overlay_original.tooltip": (
            "勾选：每句配音（含背景音）按对白时间线覆盖回原声音频——对白时段播新配音，"
            "句间等其余时段保留原声，笑声、惊叹等语气词与现场氛围因此得以保留；\n"
            "不勾选：整轨使用分离出的纯背景音配新配音，原声中的语气词不会保留。"
        ),
        "task.output_dir": "输出目录",
        "task.choose_output_dir": "选择输出目录",
        "task.subtitle.single": "单语字幕",
        "task.subtitle.bilingual_target": "双语字幕（目标语言在上）",
        "task.subtitle.bilingual_source": "双语字幕（源语言在上）",
        # ---- 语言选项（内容语言，非界面语言）----
        "lang.auto": "自动检测",
        "lang.zh": "中文",
        "lang.en": "英语",
        "lang.ja": "日语",
        "lang.ko": "韩语",
        "lang.fr": "法语",
        "lang.de": "德语",
        "lang.es": "西班牙语",
        "lang.ru": "俄语",
        "lang.ar": "阿拉伯语",
        "lang.zhen": "中英混合",
        # ---- 导入区 ----
        "import.hint_title": "拖拽视频 / 音频文件到此处",
        "import.hint_sub": "或点击此处选择文件",
        "import.hint_ext": "支持格式：视频 MKV / MP4 / MOV / AVI；音频 WAV / MP3 / FLAC / M4A",
        "import.choose_title": "选择媒体文件",
        "import.filter_media": "媒体文件",
        "import.filter_video": "视频文件",
        "import.filter_audio": "音频文件",
        "import.filter_all": "所有文件",
        "import.kind.video": "视频（{fmt}）",
        "import.kind.audio": "音频（{fmt}）",
        "import.kind.unknown": "未知类型",
        "import.duration_unknown": "未知",
        "import.col.index": "序号",
        "import.col.name": "文件名",
        "import.col.path": "文件路径",
        "import.col.type": "类型",
        "import.col.duration": "时长",
        "import.col.delete": "删除",
        "import.delete_selected": "删除所选",
        "import.confirm_delete_title": "确认删除",
        "import.confirm_delete_files": "确定从列表中删除所选的 {count} 个文件？\n{files}",
        "import.duplicate_title": "文件重复",
        "import.duplicate_found": "以下 {count} 个文件已导入（文件名与路径相同），已跳过：\n{files}",
        "import.imported_count_hint": "已导入 {count} 个文件，拖入或点击可继续添加",
        # ---- 流程进度区 ----
        "pipeline.title": "处理流程",
        "pipeline.total_progress": "总进度",
        "step.extract": "提取音频",
        "step.separate": "人声分离",
        "step.asr": "语音识别",
        "step.proofread": "文本审核",
        "step.translate": "翻译",
        "step.review_translation": "译文审核",
        "step.tts": "配音生成",
        "step.tts_review": "配音复核",
        "step.mix": "混音",
        "step.mux": "封装输出",
        # 文本转语音任务的步骤
        "step.tts_text": "文本处理",
        "step.tts_synth": "语音合成",
        "step.tts_voice_review": "语音复核",
        "step.tts_mux": "封装输出",
        # ---- 设置窗口 ----
        "settings.title": "设置",
        "settings.language": "界面语言",
        "settings.language_hint": "切换后立即生效",
        "settings.resources": "资源管理",
        "settings.builtin": "内置工具",
        "settings.builtin_hint": "随软件内置，无需下载或删除。",
        "settings.path_unknown": "（未知路径）",
        "settings.need_download": "需下载约 {size} GB",
        "settings.delete_hint": "删除后资源将恢复为未就绪（文件移入 .trash 目录，可找回）。",
        "settings.choose_download_dir": "选择下载位置",
        "settings.download_started": "开始下载 {name}…",
        "settings.download_done": "{name} 下载完成，已就绪。",
        "settings.download_failed": "{name} 下载失败：{error}",
        "settings.download_cancelled": "已取消 {name} 的下载，未完成文件移入 .trash。",
        "settings.delete_done": "已删除 {name} 的本地文件，状态恢复为未就绪。",
        "settings.delete_nothing": "{name} 没有可删除的本地文件。",
        "settings.delete_builtin": "FFmpeg 为内置工具，不支持删除。",
        "settings.downloading": "下载中 {pct}% · {speed}",
        "settings.confirm_delete_title": "确认删除",
        "settings.confirm_delete": "确定删除 {name} 的本地文件？\n文件将移入同级 .trash 目录，可手动找回。",
        # ---- ASR 校对对话框 ----
        "asr_review.title": "校对识别结果",
        "asr_review.heading": "核对识别文本",
        "asr_review.column_index": "序号",
        "asr_review.column_start": "开始时间",
        "asr_review.column_end": "结束时间",
        "asr_review.column_speaker": "说话人",
        "asr_review.column_text": "文本",
        "asr_review.column_status": "状态",
        "asr_review.column_actions": "操作",
        "asr_review.status_normal": "正常",
        "asr_review.status_deleted": "已标记删除",
        "asr_review.mark_delete": "删除",
        "asr_review.restore": "恢复",
        "asr_review.stats": "共 {total} 句 / 已标记删除 {deleted} 句 / 说话人 {speakers} 人",
        # ---- 翻译校对对话框 ----
        "translation_review.title": "校对翻译",
        "translation_review.heading": "翻译校对（{source} → {target}）",
        "translation_review.column_index": "序号",
        "translation_review.column_time": "时间",
        "translation_review.column_speaker": "说话人",
        "translation_review.column_source": "原文",
        "translation_review.column_target": "译文",
        "translation_review.column_duration": "时长(s)",
        "translation_review.column_match": "预估朗读匹配",
        "translation_review.match_pending": "待翻译",
        "translation_review.match_long": "偏长",
        "translation_review.match_short": "偏短",
        "translation_review.match_ok": "匹配",
        "translation_review.match_tip": "译文 {chars} 字符，预估朗读约 {estimated:.1f}s，可容时长 {duration:.1f}s",
        "translation_review.deleted_tooltip": "已标记删除，配音时跳过",
        "translation_review.stats": "共 {total} 句 / 已标记删除 {deleted} 句（配音时跳过）",
        # ---- TTS 复核对话框 ----
        "tts_review.title": "复核配音",
        "tts_review.heading": "复核配音",
        "tts_review.regenerate_all": "重生成所有异常句",
        "tts_review.regenerate_selected": "重生成所选句",
        "tts_review.column_index": "序号",
        "tts_review.column_time": "时间",
        "tts_review.column_speaker": "说话人",
        "tts_review.column_source": "原文",
        "tts_review.column_target": "译文",
        "tts_review.column_ratio": "伸缩比",
        "tts_review.column_listen": "试听",
        "tts_review.column_regen": "重新生成",
        "tts_review.listen": "试听",
        "tts_review.continuous_listen": "连续试听",
        "tts_review.regenerate_this": "重新生成此句",
        "tts_review.stats": "共 {total} 句，伸缩比异常 {abnormal} 句 / 待重生成 {pending} 句",
        "tts_review.regen_running": "生成中…",
        "tts_review.regen_progress": "生成中… {percent}%",
        "tts_review.regen_waiting": "等待重生成",
        "tts_review.regen_failed_title": "重生成失败",
        "tts_review.regen_failed": "第 {index} 句重生成失败：{error}",
        "tts_review.regen_unavailable": "TTS 引擎不可用，无法重生成",
        # ---- 说话人音色管理对话框 ----
        "voice.title": "管理说话人音色",
        "voice.heading": "说话人音色管理",
        "voice.hint": "为每位说话人选择参考音频 wav，用于 TTS 音色克隆；点击“更换”可重新选择。",
        "voice.speaker_count": "{spk}（{count} 句）",
        "voice.sample": "例句：{text}",
        "voice.listen": "试听",
        "voice.change": "更换",
        "voice.no_ref": "（未设置参考音频）",
        "voice.no_sample": "（无例句）",
        "voice.choose_ref_title": "选择 {spk} 的参考音频",
        "voice.filter_audio": "音频文件",
        "voice.filter_all": "所有文件",
        # ---- 任务队列（主窗口任务列表） ----
        "queue.title": "任务列表",
        "queue.add_dub": "＋ 配音与字幕",
        "queue.add_sub": "＋ 生成字幕",
        "queue.add_tts": "＋ 文本转语音",
        "queue.cat.dub": "配音与字幕",
        "queue.cat.sub": "生成字幕",
        "queue.cat.tts": "文本转语音",
        "queue.delete": "删除任务",
        "queue.col.index": "序号",
        "queue.col.name": "任务名称",
        "queue.col.category": "类别",
        "queue.col.status": "状态",
        "queue.col.progress": "进度",
        "queue.col.delete": "删除",
        "queue.confirm_delete_title": "确认删除",
        "queue.confirm_delete_tasks": (
            "确定删除所选的 {count} 个任务？\n{names}\n"
            "排队中的任务将被移出队列，已生成的产物文件不受影响。"
        ),
        "queue.delete_running": "运行中的任务无法删除，请先停止处理。",
        "queue.delete_running_skipped": "{count} 个运行中的任务无法删除，已跳过（请先停止处理）。",
        "queue.state.queued": "排队中",
        "queue.state.running": "运行中",
        "queue.state.cancelling": "正在取消",
        "queue.state.done": "已完成",
        "queue.state.failed": "失败",
        "queue.state.cancelled": "已取消",
        "queue.state.interrupted": "已中断",
        "queue.resume": "继续",
        "queue.stop": "停止",
        "queue.resume_title": "继续执行",
        "queue.resume_confirm": (
            "将把 {count} 个未完成的任务重新入列执行（已完成的流程自动跳过）。"
            "确定继续吗？"
        ),
        "queue.resume_started": "已入列 {count} 个任务，等待执行。",
        "queue.resume_none": "没有可继续执行的任务。",
        "queue.menu.cancel": "取消排队",
        "queue.menu.retry": "重试任务",
        "queue.menu.cleanup": "清理中间文件",
        "queue.menu.open_folder": "打开输出文件夹",
        "queue.menu.remove": "移除记录",
        "queue.duplicate": "任务 {name} 已在队列中，请勿重复添加。",
        # ---- 任务窗口 ----
        "taskwindow.new_dubbing": "新建配音与字幕任务",
        "taskwindow.new_subtitle": "新建生成字幕任务",
        "taskwindow.new_tts": "新建文本转语音任务",
        "taskwindow.auto_accept": "自动通过人工审核（挂机批量）",
        "taskwindow.auto_accept_tip": (
            "勾选后，人工审核 / 译文审核 / 音色管理 / 配音复听将直接采纳当前结果，"
            "不再暂停等待确认。"
        ),
        "taskwindow.cleanup": "清理中间文件",
        "taskwindow.cancel_queue": "取消排队",
        "taskwindow.cancelling": "正在取消…",
        "taskwindow.reprocess": "重新开始",
        "taskwindow.cleanup_no_products": "（未发现成品文件）",
        # ---- 文本转语音任务窗口 ----
        "tts.text_title": "文本内容",
        "tts.text_placeholder": "输入或粘贴文本，按句合成语音…",
        "tts.choose_txt_title": "选择文本文件",
        "tts.txt_filter": "文本文件",
        "tts.drop_hint_title": "拖入 TXT 文本",
        "tts.drop_hint_sub": "可一次拖入多个文件，批量创建任务",
        "tts.drop_hint_ext": "仅支持 TXT 文本（*.txt）",
        "tts.txt_loaded": "已载入文本：{name}（{count} 字）",
        "tts.col.chars": "字数",
        "tts.col.emotion": "情绪",
        "tts.empty_txt_title": "空文本文件",
        "tts.empty_txt_text": "文件 {name} 内容为空或无法读取，已跳过。",
        "tts.manual_hint": "点击此处，输入或粘贴文本",
        "tts.manual_takes_over": "已切换为手动输入，清空已载入的 TXT 文件",
        "tts.char_count": "{count} 字 · {sentences} 句",
        "tts.ref_title": "参考音频",
        "tts.ref_unset": "（未选择参考音频）",
        "tts.choose_ref": "选择参考音频",
        "tts.choose_ref_title": "选择参考音频文件",
        "tts.ref_filter": "音频文件",
        "tts.task_name": "任务名",
        "tts.task_name_hint": "用作成品文件名与工作夹名",
        "tts.language": "文本语言",
        "tts.lang_auto_detected": "自动识别（{lang}）",
        "tts.speed": "语速",
        "tts.generate_srt": "生成 SRT 字幕",
        "tts.output_format": "输出格式",
        "tts.format.wav": "WAV 无损",
        "tts.format.mp3": "MP3 通用",
        "tts.format.m4a": "M4A（AAC）",
        "tts.format.flac": "FLAC 无损",
        "tts.edit": "编辑…",
        "tts.col.edit": "编辑",
        "tts.emo_follow_all": "跟随参考音频",
        "tts.emo_custom_count": "{count} 句已定制",
        "tts.edit_tip": "点击编辑该文件的句子与情绪",
        "tts.polyphone_hint": "多音字可用 <文字|发音> 标注，如 <行|XING2>",
        "tts.name_follow_files": "多文件模式下任务名跟随文件名，不可手动输入",
        "tts.no_text_for_edit": "请先输入文本（分句后为空）。",
        "tts.name_required": "请填写任务名。",
        "tts.text_required": "请输入需要合成语音的文本。",
        "tts.ref_required": "请选择参考音频（声音克隆的音色来源）。",
        "tts.langid_missing_hint": (
            "未安装 langid 库，无法自动识别文本语言，将按中文合成"
            "（可 pip install langid）。"
        ),
        "tts.unsupported_lang_title": "语种不受原生支持",
        "tts.unsupported_lang_text": (
            "文本语言为{lang}。IndexTTS-2.5 原生支持中文 / 英语 / 日语 / "
            "西班牙语 / 阿拉伯语（及中英混合）；该语种将由模型回退通用合成，"
            "发音效果可能不佳。是否继续？"
        ),
        # ---- 情绪选项（文本转语音） ----
        "emo.follow": "跟随参考音频",
        "emo.happy": "开心",
        "emo.calm": "平静",
        "emo.sad": "悲伤",
        "emo.angry": "愤怒",
        "emo.afraid": "恐惧",
        "emo.disgusted": "厌恶",
        "emo.melancholic": "低落",
        "emo.surprised": "惊讶",
        # ---- 编辑对话框（文本转语音：改句 / 选情绪 / 删句） ----
        "edit_dialog.title": "编辑",
        "edit_dialog.tip": (
            "直接修改内容列文本可改写句子；逐句选择合成情绪（默认跟随参考音频）；"
            "不需要的句子点删除移出（可再点恢复）。"
        ),
        "edit_dialog.col.index": "序号",
        "edit_dialog.col.text": "内容",
        "edit_dialog.col.emotion": "情绪",
        "edit_dialog.col.delete": "删除",
        "edit_dialog.restore": "恢复",
        "edit_dialog.deleted": "（已删除）",
        # ---- 文本转语音流程引擎 ----
        "tts_runner.started": "开始文本转语音：{name}",
        "tts_runner.no_text": "文本分句后为空，无法合成。",
        "tts_runner.sentences": "分句完成：共 {count} 句（语言：{lang}）",
        "tts_runner.lang_detect_failed": "未能识别文本语言，按中文合成。",
        "tts_runner.lang_fallback": "语种 {lang} 非原生支持，由模型回退通用合成。",
        "tts_runner.synth_progress": "语音合成 {done}/{total} 句",
        "tts_runner.synth_progress_pct": "语音合成 {done}/{total} 句（{pct}%）",
        "tts_runner.synth_incomplete": "合成记录与分句数量不一致，请重试。",
        "tts_runner.sample_rate_mismatch": "合成音频采样率异常（{sr}Hz），请重试。",
        "tts_runner.ffmpeg_missing": "未找到内置 FFmpeg，无法转换输出格式。",
        "tts_runner.format_failed": "转换 {fmt} 失败：{error}",
        "tts_runner.audio_done": "音频已生成：{path}（约 {seconds} 秒）",
        "tts_runner.review_start": "语音复核：试听全部语音，可删除或重生成不满意的句子。",
        "tts_runner.review_auto": "已勾选自动通过人工审核，跳过语音复核。",
        "tts_runner.review_done": "语音复核完成：保留 {kept} 句，删除 {deleted} 句。",
        "tts_runner.all_deleted": "全部句子已在复核中删除，没有可生成的成品。",
        # ---- 语音复核对话框（文本转语音） ----
        "tts_voice_review.title": "复核语音",
        "tts_voice_review.heading": "复核语音",
        "tts_voice_review.tip": "试听各句合成结果；不满意的句子可重生成，或点击情绪列直接更换情绪（自动重新生成该句），不需要的句子可删除（不进入成品）。已删除的句子可再点恢复。",
        "tts_voice_review.column_index": "序号",
        "tts_voice_review.column_time": "时间",
        "tts_voice_review.column_content": "内容",
        "tts_voice_review.column_emotion": "情绪",
        "tts_voice_review.column_listen": "试听",
        "tts_voice_review.column_delete": "删除",
        "tts_voice_review.column_regen": "重生成",
        "tts_voice_review.listen": "试听",
        "tts_voice_review.continuous_listen": "连续试听",
        "tts_voice_review.restore": "恢复",
        "tts_voice_review.regenerate_this": "重新生成",
        "tts_voice_review.emotion_tooltip": "点击更换情绪（自动重新生成该句）",
        "tts_voice_review.regen_waiting": "等待重生成",
        "tts_voice_review.regen_running": "生成中…",
        "tts_voice_review.regen_progress": "生成中… {percent}%",
        "tts_voice_review.regen_failed_title": "重生成失败",
        "tts_voice_review.regen_failed": "第 {index} 句重生成失败：{error}",
        "tts_voice_review.regen_unavailable": "TTS 引擎不可用，无法重生成",
        "tts_voice_review.stats": "共 {total} 句，已删除 {deleted} 句 / 待重生成 {pending} 句",
        # ---- 系统监视栏 ----
        "monitor.cpu": "CPU",
        "monitor.memory": "内存",
        "monitor.gpu": "GPU",
        # ---- 流程结果查看窗口 ----
        "viewer.open_folder": "打开文件夹",
        "viewer.no_data": "该流程还没有可查看的结果文件。",
        "viewer.track.extracted": "提取的音频",
        "viewer.track.original_full": "分离前（原始音频）",
        "viewer.track.vocals": "人声",
        "viewer.track.background": "背景音",
        "viewer.track.original": "原音频（提取音频）",
        "viewer.track.mixed": "混音后音频",
        "viewer.video.original": "原视频",
        "viewer.video.dubbed": "配音后视频",
        "viewer.video.unavailable": (
            "无法播放视频（播放组件不可用或文件不存在）。\n{path}"
        ),
        "viewer.video.play_error": "视频播放失败：{error}\n请通过“打开文件夹”查看文件。",
        "viewer.tts.original": "原声",
        "viewer.tts.dubbed": "配音",
        "viewer.continue": "继续",
        "viewer.continue_title": "继续执行",
        "viewer.continue_confirm": (
            "即将从「{stage}」开始重新执行，该流程及其之后生成的内容将被覆盖。"
            "确定开始吗？"
        ),
        "viewer.subtitle_panel": "字幕",
        "viewer.subtitle_none": "未生成字幕",
        "viewer.tts.ref_audio": "参考音频",
        "viewer.tts.change_ref": "更换参考音频",
        "viewer.tts.change_ref_title": "选择参考音频文件",
        "viewer.tts.ref_changed": "参考音频已更换：下次执行时将从头重新合成。",
        "viewer.tts.final_audio": "成品音频",
        "viewer.tts.lang": "语言：{lang}",
        # ---- 音频试听条 ----
        "player.play": "试听",
        "player.stop": "停止",
        "player.no_audio": "未加载音频",
        "player.file_missing": "音频文件不存在",
        "player.playing_basic": "播放中（基础模式）",
    },
    "en": {
        # ---- Common ----
        "common.ready": "Ready",
        "common.not_ready": "Not ready",
        "common.close": "Close",
        "common.cancel": "Cancel",
        "common.confirm": "OK",
        "common.settings": "Settings",
        "common.delete": "Delete",
        "common.download": "Download",
        "common.model_homepage": "Model homepage",
        # ---- Main window ----
        "main.title": "Minim SubDub",
        "main.start": "Start",
        "main.start_batch": "Start batch",
        "main.log.batch_started": "Batch started: {count} files in total",
        "main.log.batch_queued": "The other {count} files were added to the task queue",
        "main.log.init": "Initialized: {n}/{total} resources ready (FFmpeg is built in; other models need downloading).",
        "main.log.device_initial": "Initial device detection: {desc}",
        "main.log.device_done": "Device detection complete: {desc}",
        "main.log.imported": "File imported: {name}",
        "main.log.no_file": "Please import a media file first.",
        "main.log.task_settings": "Source {src} → Target {dst}",
        "main.log.missing_resources": "Resources not ready, please download them in Settings first: {names}",
        "main.log.llm_server_missing": "llama-server not found (on macOS install via brew install llama.cpp); translation is unavailable.",
        "main.log.finished": "Finished: {outputs}",
        "main.log.cancelled": "Cancelled (outputs of finished stages are kept; restart to resume).",
        "main.log.failed": "Failed: {error}",
        "main.stop": "Stop",
        "main.log.cancelling": "Cancelling, waiting for the current step to finish…",
        "main.close_confirm_title": "Confirm close",
        "main.close_confirm_text": "Tasks are still running. Interrupt them and close the window?",
        "main.cleanup_title": "Done",
        "main.cleanup_question": "Task finished. Delete intermediate temporary files?\nThese outputs will be kept:\n{files}",
        "main.cleanup_delete": "Delete temp files",
        "main.cleanup_keep": "Keep for later",
        "main.cleanup_done": "Temporary files cleaned; outputs kept in the work directory.",
        "main.voice_clone_title": "Voice cloning notice",
        "main.voice_clone_text": (
            "You are about to use voice cloning to generate dubbing. Please confirm you "
            "have the legal right to use the voice in the original audio and that the "
            "generated dubbing is for lawful use only."
        ),
        "main.voice_clone_agree": "I confirm I have the right",
        # ---- Pipeline engine ----
        "runner.started": "Processing started: {name}",
        "runner.stage_skipped": "Skipped {stage} (reusing existing outputs)",
        "runner.lang_detected": "Detected source language: {lang}",
        "runner.homura_fix": "Length fix: {overlong} segments projected overlong, {fixed} re-compressed (the rest handled by TTS stretch)",
        "runner.device_fallback": "{stage} accelerated inference failed ({error}); retrying on CPU",
        "runner.srt_done": "Subtitles generated: {path} ({count} entries)",
        "runner.mux_skipped_audio_input": "Audio input: MKV muxing skipped (mixed wav and subtitles archived to the mux folder)",
        "runner.voice_picked": "Reference voices picked for {count} speakers",
        "runner.voice_reused": "Reusing confirmed reference voices",
        "runner.voice_none": "Failed to pick reference voices (no long-enough valid speech); check the separation result",
        "runner.seg_ref_ready": "Sentence slices ready: {own} segments use their own slice as reference (preserving emotion), {fallback} fall back to the speaker reference",
        "runner.tts_no_ref": "No usable reference audio for a segment of speaker {spk}; skipped",
        "runner.tts_progress": "Generating dubbing {done}/{total}",
        "runner.tts_progress_pct": "Generating dubbing {done}/{total} ({pct}%)",
        "runner.tts_review_needed": "Dubbing review: {count} segments; listen, edit translations, and regenerate on the spot",
        "runner.srt_synced": "{count} translations edited during dubbing review; subtitles regenerated",
        "runner.mix_overlay": "Mixing over the original audio: dialogues use the new dub, gaps keep the original (interjections preserved)",
        "runner.tts_env_missing": "TTS runtime environment not ready: {detail} (IndexTTS-2.5 requires .venv-tts and sources in third_party/index-tts)",
        "runner.auto_review_passed": "Reviews auto-accepted (unattended batch mode)",
        # ---- Resource bar ----
        "bar.device_title": "Inference device",
        "bar.device_value": "{value}",
        "bar.env_tts": "TTS runtime",
        "bar.env_llama": "LLAMA service",
        "resource.ffmpeg.feature": "Audio extraction",
        "resource.demucs.feature": "Vocal separation",
        "resource.transcribe.feature": "Speech-to-text",
        "resource.translate.feature": "Subtitle translation",
        "resource.homura.feature": "Dubbing duration correction",
        "resource.tts.feature": "Voice cloning",
        # ---- Runtime environment setup ----
        "settings.env_feature": "Runtime environment",
        "settings.env_sub": "TTS virtualenv · LLAMA service",
        "settings.env_ready_detail": ".venv-tts and llama-server are both ready",
        "settings.env_missing_detail": "TTS virtualenv or llama-server missing; click \"Set up\" on the right",
        "settings.env_setup": "Set up",
        "env.setup_started": "Setting up the runtime environment (TTS virtualenv + LLAMA service)…",
        "env.setup_done": "Runtime environment setup finished: {detail}",
        "env.setup_failed": "Runtime environment setup failed: {error}",
        "env.setup_cancelled": "Setup cancelled; completed parts will be skipped next time.",
        "env.step.check_tts": "Checking the TTS runtime…",
        "env.step.venv": "Creating the .venv-tts virtual environment…",
        "env.step.pth": "Sharing main-env dependencies (torch etc.)…",
        "env.step.clone": "Cloning IndexTTS sources (via gh-proxy)…",
        "env.step.pip_core": "Installing core dependencies (transformers 4.52 / torchaudio 2.8)…",
        "env.step.pip_runtime": "Installing IndexTTS runtime dependencies…",
        "env.step.pip_sub": "Installing audio sub-dependencies (matplotlib / tensorboard etc.)…",
        "env.step.strip": "Stripping torchaudio C extensions (for shared torch)…",
        "env.step.verify": "Verifying imports…",
        "env.step.llama_api": "Fetching the latest llama.cpp release…",
        "env.step.llama_download": "Downloading llama-server",
        "env.step.llama_extract": "Extracting llama-server…",
        # ---- Device descriptions ----
        "device.cuda": "CUDA ({name})",
        "device.mps": "MPS (Apple Silicon accelerated)",
        "device.cpu": "CPU (slow)",
        # ---- Task settings ----
        "task.title": "Task settings",
        "task.source_language": "Source language",
        "task.target_language": "Target language",
        "task.subtitle_mode": "Subtitle format",
        "task.overlay_original": "Lay dubbing over original audio",
        "task.overlay_original.tooltip": (
            "Checked: each dubbed line (with background) is laid over the original audio on its "
            "dialogue timeline — dialogues play the new dub while everything in between keeps the "
            "original sound, so laughter, gasps and other interjections survive;\n"
            "Unchecked: the whole track uses the separated background only, and those "
            "interjections are lost."
        ),
        "task.output_dir": "Output directory",
        "task.choose_output_dir": "Choose output directory",
        "task.subtitle.single": "Monolingual subtitles",
        "task.subtitle.bilingual_target": "Bilingual (target language on top)",
        "task.subtitle.bilingual_source": "Bilingual (source language on top)",
        # ---- Content language options ----
        "lang.auto": "Auto-detect",
        "lang.zh": "Chinese",
        "lang.en": "English",
        "lang.ja": "Japanese",
        "lang.ko": "Korean",
        "lang.fr": "French",
        "lang.de": "German",
        "lang.es": "Spanish",
        "lang.ru": "Russian",
        "lang.ar": "Arabic",
        "lang.zhen": "Mixed Chinese-English",
        # ---- Import area ----
        "import.hint_title": "Drag a video / audio file here",
        "import.hint_sub": "or click here to browse",
        "import.hint_ext": "Supported: video MKV / MP4 / MOV / AVI; audio WAV / MP3 / FLAC / M4A",
        "import.choose_title": "Choose a media file",
        "import.filter_media": "Media files",
        "import.filter_video": "Video files",
        "import.filter_audio": "Audio files",
        "import.filter_all": "All files",
        "import.kind.video": "Video ({fmt})",
        "import.kind.audio": "Audio ({fmt})",
        "import.kind.unknown": "Unknown type",
        "import.duration_unknown": "unknown",
        "import.col.index": "No.",
        "import.col.name": "File name",
        "import.col.path": "Path",
        "import.col.type": "Type",
        "import.col.duration": "Duration",
        "import.col.delete": "Delete",
        "import.delete_selected": "Delete selected",
        "import.confirm_delete_title": "Confirm deletion",
        "import.confirm_delete_files": "Remove the {count} selected file(s) from the list?\n{files}",
        "import.duplicate_title": "Duplicate files",
        "import.duplicate_found": "{count} file(s) already imported (same name and path); skipped:\n{files}",
        "import.imported_count_hint": "{count} files imported — drag or click to add more",
        # ---- Pipeline panel ----
        "pipeline.title": "Pipeline",
        "pipeline.total_progress": "Overall progress",
        "step.extract": "Extract audio",
        "step.separate": "Separate vocals",
        "step.asr": "Transcribe",
        "step.proofread": "Text review",
        "step.translate": "Translate",
        "step.review_translation": "Review translation",
        "step.tts": "Generate dubbing",
        "step.tts_review": "Review dubbing",
        "step.mix": "Mix",
        "step.mux": "Mux output",
        # Steps of the text-to-speech task
        "step.tts_text": "Text processing",
        "step.tts_synth": "Synthesize speech",
        "step.tts_voice_review": "Voice review",
        "step.tts_mux": "Mux output",
        # ---- Settings dialog ----
        "settings.title": "Settings",
        "settings.language": "Interface language",
        "settings.language_hint": "Applied immediately",
        "settings.resources": "Resources",
        "settings.builtin": "Built-in",
        "settings.builtin_hint": "Bundled with the app; no download or deletion needed.",
        "settings.path_unknown": "(unknown path)",
        "settings.need_download": "Requires ~{size} GB download",
        "settings.delete_hint": "After deletion the resource becomes not ready (files are moved to a .trash folder and can be recovered).",
        "settings.choose_download_dir": "Choose download location",
        "settings.download_started": "Downloading {name}…",
        "settings.download_done": "{name} download complete, now ready.",
        "settings.download_failed": "{name} download failed: {error}",
        "settings.download_cancelled": "Download of {name} cancelled; incomplete files moved to .trash.",
        "settings.delete_done": "Local files of {name} deleted; status is now not ready.",
        "settings.delete_nothing": "{name} has no local files to delete.",
        "settings.delete_builtin": "FFmpeg is built in and cannot be deleted.",
        "settings.downloading": "Downloading {pct}% · {speed}",
        "settings.confirm_delete_title": "Confirm deletion",
        "settings.confirm_delete": "Delete local files of {name}?\nFiles will be moved to a .trash folder next to them and can be recovered manually.",
        # ---- ASR review dialog ----
        "asr_review.title": "Review transcription",
        "asr_review.heading": "Check transcribed text",
        "asr_review.column_index": "No.",
        "asr_review.column_start": "Start",
        "asr_review.column_end": "End",
        "asr_review.column_speaker": "Speaker",
        "asr_review.column_text": "Text",
        "asr_review.column_status": "Status",
        "asr_review.column_actions": "Actions",
        "asr_review.status_normal": "Normal",
        "asr_review.status_deleted": "Marked deleted",
        "asr_review.mark_delete": "Mark deleted",
        "asr_review.restore": "Restore",
        "asr_review.stats": "{total} segments / {deleted} marked deleted / {speakers} speakers",
        # ---- Translation review dialog ----
        "translation_review.title": "Review translation",
        "translation_review.heading": "Translation review ({source} → {target})",
        "translation_review.column_index": "No.",
        "translation_review.column_time": "Time",
        "translation_review.column_speaker": "Speaker",
        "translation_review.column_source": "Source",
        "translation_review.column_target": "Translation",
        "translation_review.column_duration": "Length (s)",
        "translation_review.column_match": "Speech fit",
        "translation_review.match_pending": "Untranslated",
        "translation_review.match_long": "Too long",
        "translation_review.match_short": "Too short",
        "translation_review.match_ok": "Fits",
        "translation_review.match_tip": "Translation is {chars} chars; estimated speech {estimated:.1f}s, available {duration:.1f}s",
        "translation_review.deleted_tooltip": "Marked deleted; skipped during dubbing",
        "translation_review.stats": "{total} segments / {deleted} marked deleted (skipped during dubbing)",
        # ---- TTS review dialog ----
        "tts_review.title": "Review dubbing",
        "tts_review.heading": "Review dubbing",
        "tts_review.regenerate_all": "Regenerate all abnormal",
        "tts_review.regenerate_selected": "Regenerate selected",
        "tts_review.column_index": "No.",
        "tts_review.column_time": "Time",
        "tts_review.column_speaker": "Speaker",
        "tts_review.column_source": "Source",
        "tts_review.column_target": "Translation",
        "tts_review.column_ratio": "Stretch",
        "tts_review.column_listen": "Play",
        "tts_review.column_regen": "Regenerate",
        "tts_review.listen": "Play",
        "tts_review.continuous_listen": "Play all",
        "tts_review.regenerate_this": "Regenerate this segment",
        "tts_review.stats": "{total} segments, {abnormal} abnormal / {pending} pending regeneration",
        "tts_review.regen_running": "Generating…",
        "tts_review.regen_progress": "Generating… {percent}%",
        "tts_review.regen_waiting": "Waiting",
        "tts_review.regen_failed_title": "Regeneration failed",
        "tts_review.regen_failed": "Segment {index} failed to regenerate: {error}",
        "tts_review.regen_unavailable": "TTS engine unavailable; cannot regenerate",
        # ---- Speaker voice management dialog ----
        "voice.title": "Manage speaker voices",
        "voice.heading": "Speaker voice management",
        "voice.hint": "Choose a reference audio (wav) for each speaker for TTS voice cloning; click \"Change\" to reselect.",
        "voice.speaker_count": "{spk} ({count} segments)",
        "voice.sample": "Sample: {text}",
        "voice.listen": "Play",
        "voice.change": "Change",
        "voice.no_ref": "(no reference audio set)",
        "voice.no_sample": "(no sample)",
        "voice.choose_ref_title": "Choose reference audio for {spk}",
        "voice.filter_audio": "Audio files",
        "voice.filter_all": "All files",
        # ---- Task queue (main window task list) ----
        "queue.title": "Task list",
        "queue.add_dub": "+ Dub & subtitles",
        "queue.add_sub": "+ Subtitles",
        "queue.add_tts": "+ Text to speech",
        "queue.cat.dub": "Dub & subtitles",
        "queue.cat.sub": "Subtitles",
        "queue.cat.tts": "Text to speech",
        "queue.delete": "Delete task",
        "queue.col.index": "No.",
        "queue.col.name": "Task",
        "queue.col.category": "Category",
        "queue.col.status": "Status",
        "queue.col.progress": "Progress",
        "queue.col.delete": "Delete",
        "queue.confirm_delete_title": "Confirm deletion",
        "queue.confirm_delete_tasks": (
            "Delete the {count} selected task(s)?\n{names}\n"
            "Queued tasks will be removed from the queue; generated output files are not affected."
        ),
        "queue.delete_running": "Running tasks cannot be deleted; stop them first.",
        "queue.delete_running_skipped": "{count} running task(s) cannot be deleted and were skipped (stop them first).",
        "queue.state.queued": "Queued",
        "queue.state.running": "Running",
        "queue.state.cancelling": "Cancelling",
        "queue.state.done": "Done",
        "queue.state.failed": "Failed",
        "queue.state.cancelled": "Cancelled",
        "queue.state.interrupted": "Interrupted",
        "queue.resume": "Resume",
        "queue.stop": "Stop",
        "queue.resume_title": "Resume tasks",
        "queue.resume_confirm": (
            "Re-queue {count} unfinished task(s) (finished stages are skipped "
            "automatically). Continue?"
        ),
        "queue.resume_started": "{count} task(s) re-queued, waiting to run.",
        "queue.resume_none": "No tasks to resume.",
        "queue.menu.cancel": "Cancel queueing",
        "queue.menu.retry": "Retry",
        "queue.menu.cleanup": "Clean up temp files",
        "queue.menu.open_folder": "Open output folder",
        "queue.menu.remove": "Remove from list",
        "queue.duplicate": "Task {name} is already in the queue.",
        # ---- Task window ----
        "taskwindow.new_dubbing": "New dubbing & subtitles task",
        "taskwindow.new_subtitle": "New subtitles task",
        "taskwindow.new_tts": "New text-to-speech task",
        "taskwindow.auto_accept": "Auto-accept reviews (unattended batch)",
        "taskwindow.auto_accept_tip": (
            "When checked, human review steps accept the current results "
            "automatically without pausing."
        ),
        "taskwindow.cleanup": "Clean up temp files",
        "taskwindow.cancel_queue": "Cancel queueing",
        "taskwindow.cancelling": "Cancelling…",
        "taskwindow.reprocess": "Start again",
        "taskwindow.cleanup_no_products": "(no output files found)",
        # ---- Text-to-speech task window ----
        "tts.text_title": "Text",
        "tts.text_placeholder": "Type or paste text; speech is synthesized sentence by sentence…",
        "tts.choose_txt_title": "Choose a text file",
        "tts.txt_filter": "Text files",
        "tts.drop_hint_title": "Drop TXT files",
        "tts.drop_hint_sub": "Drop multiple files to create tasks in batch",
        "tts.drop_hint_ext": "TXT documents only (*.txt)",
        "tts.txt_loaded": "Text loaded: {name} ({count} chars)",
        "tts.col.chars": "Chars",
        "tts.col.emotion": "Emotion",
        "tts.empty_txt_title": "Empty text file",
        "tts.empty_txt_text": "File {name} is empty or unreadable and was skipped.",
        "tts.manual_hint": "Click here to type or paste text",
        "tts.manual_takes_over": "Switched to manual input; loaded TXT files cleared",
        "tts.char_count": "{count} chars · {sentences} sentences",
        "tts.ref_title": "Reference audio",
        "tts.ref_unset": "(no reference audio selected)",
        "tts.choose_ref": "Choose reference audio",
        "tts.choose_ref_title": "Choose a reference audio file",
        "tts.ref_filter": "Audio files",
        "tts.task_name": "Task name",
        "tts.task_name_hint": "Used as the output file name and work folder name",
        "tts.language": "Text language",
        "tts.lang_auto_detected": "Auto-detect ({lang})",
        "tts.speed": "Speech rate",
        "tts.generate_srt": "Generate SRT subtitles",
        "tts.output_format": "Output format",
        "tts.format.wav": "WAV lossless",
        "tts.format.mp3": "MP3 general",
        "tts.format.m4a": "M4A (AAC)",
        "tts.format.flac": "FLAC lossless",
        "tts.edit": "Edit…",
        "tts.col.edit": "Edit",
        "tts.emo_follow_all": "Follow reference audio",
        "tts.emo_custom_count": "{count} sentences customized",
        "tts.edit_tip": "Click to edit sentences and emotions of this file",
        "tts.polyphone_hint": "Mark polyphones with <text|pinyin>, e.g. <行|XING2>",
        "tts.name_follow_files": "In multi-file mode task names follow file names",
        "tts.no_text_for_edit": "Please enter some text first (sentence split is empty).",
        "tts.name_required": "Please enter a task name.",
        "tts.text_required": "Please enter the text to synthesize.",
        "tts.ref_required": "Please choose reference audio (the voice source for cloning).",
        "tts.langid_missing_hint": (
            "The langid package is missing; text language cannot be auto-detected "
            "and Chinese will be used (pip install langid)."
        ),
        "tts.unsupported_lang_title": "Language not natively supported",
        "tts.unsupported_lang_text": (
            "The text language is {lang}. IndexTTS-2.5 natively supports Chinese / English / "
            "Japanese / Spanish / Arabic (plus mixed Chinese-English); this language will "
            "fall back to generic synthesis and the pronunciation may be poor. Continue?"
        ),
        # ---- Emotion options (text to speech) ----
        "emo.follow": "Follow reference audio",
        "emo.happy": "Happy",
        "emo.calm": "Calm",
        "emo.sad": "Sad",
        "emo.angry": "Angry",
        "emo.afraid": "Afraid",
        "emo.disgusted": "Disgusted",
        "emo.melancholic": "Melancholic",
        "emo.surprised": "Surprised",
        # ---- Edit dialog (text to speech: edit text / pick emotion / delete) ----
        "edit_dialog.title": "Edit",
        "edit_dialog.tip": (
            "Edit the text column to rewrite a sentence; pick the emotion per "
            "sentence (default: follow the reference audio); delete sentences "
            "you do not need (click again to restore)."
        ),
        "edit_dialog.col.index": "No.",
        "edit_dialog.col.text": "Text",
        "edit_dialog.col.emotion": "Emotion",
        "edit_dialog.col.delete": "Delete",
        "edit_dialog.restore": "Restore",
        "edit_dialog.deleted": "(deleted)",
        # ---- Text-to-speech pipeline engine ----
        "tts_runner.started": "Text-to-speech started: {name}",
        "tts_runner.no_text": "Sentence split is empty; nothing to synthesize.",
        "tts_runner.sentences": "Sentence split done: {count} sentences (language: {lang})",
        "tts_runner.lang_detect_failed": "Text language not detected; synthesizing as Chinese.",
        "tts_runner.lang_fallback": "{lang} is not natively supported; falling back to generic synthesis.",
        "tts_runner.synth_progress": "Synthesizing {done}/{total}",
        "tts_runner.synth_progress_pct": "Synthesizing {done}/{total} ({pct}%)",
        "tts_runner.synth_incomplete": "Synthesis records do not match the sentence count; please retry.",
        "tts_runner.sample_rate_mismatch": "Unusual sample rate ({sr}Hz); please retry.",
        "tts_runner.ffmpeg_missing": "Built-in FFmpeg not found; cannot convert the output format.",
        "tts_runner.format_failed": "Failed to convert to {fmt}: {error}",
        "tts_runner.audio_done": "Audio generated: {path} (about {seconds}s)",
        "tts_runner.review_start": "Voice review: listen to each sentence; regenerate or delete the ones you are not satisfied with.",
        "tts_runner.review_auto": "Auto-accept reviews is checked; voice review skipped.",
        "tts_runner.review_done": "Voice review done: {kept} kept, {deleted} deleted.",
        "tts_runner.all_deleted": "All sentences were deleted during review; nothing to generate.",
        # ---- Voice review dialog (text to speech) ----
        "tts_voice_review.title": "Review speech",
        "tts_voice_review.heading": "Review speech",
        "tts_voice_review.tip": "Listen to each sentence; regenerate the unsatisfactory ones, click the Emotion cell to change its emotion (the sentence is regenerated automatically), and delete the ones you do not need (they will not enter the final output). Deleted sentences can be restored.",
        "tts_voice_review.column_index": "No.",
        "tts_voice_review.column_time": "Time",
        "tts_voice_review.column_content": "Text",
        "tts_voice_review.column_emotion": "Emotion",
        "tts_voice_review.column_listen": "Listen",
        "tts_voice_review.column_delete": "Delete",
        "tts_voice_review.column_regen": "Regenerate",
        "tts_voice_review.listen": "Listen",
        "tts_voice_review.continuous_listen": "Play all",
        "tts_voice_review.restore": "Restore",
        "tts_voice_review.regenerate_this": "Regenerate",
        "tts_voice_review.emotion_tooltip": "Click to change the emotion (regenerates this sentence)",
        "tts_voice_review.regen_waiting": "Waiting",
        "tts_voice_review.regen_running": "Generating…",
        "tts_voice_review.regen_progress": "Generating… {percent}%",
        "tts_voice_review.regen_failed_title": "Regeneration failed",
        "tts_voice_review.regen_failed": "Sentence {index} failed to regenerate: {error}",
        "tts_voice_review.regen_unavailable": "TTS engine unavailable; cannot regenerate",
        "tts_voice_review.stats": "{total} sentences, {deleted} deleted / {pending} pending regeneration",
        # ---- System monitor bar ----
        "monitor.cpu": "CPU",
        "monitor.memory": "Memory",
        "monitor.gpu": "GPU",
        # ---- Stage result viewer windows ----
        "viewer.open_folder": "Open folder",
        "viewer.no_data": "No result files to show for this stage yet.",
        "viewer.track.extracted": "Extracted audio",
        "viewer.track.original_full": "Before separation (original audio)",
        "viewer.track.vocals": "Vocals",
        "viewer.track.background": "Background music",
        "viewer.track.original": "Original audio (extracted)",
        "viewer.track.mixed": "Mixed audio",
        "viewer.video.original": "Original video",
        "viewer.video.dubbed": "Dubbed video",
        "viewer.video.unavailable": (
            "Cannot play video (player unavailable or file missing).\n{path}"
        ),
        "viewer.video.play_error": "Video playback failed: {error}\nUse \"Open folder\" to view the file.",
        "viewer.tts.original": "Original speech",
        "viewer.tts.dubbed": "Dubbing",
        "viewer.continue": "Continue",
        "viewer.continue_title": "Continue task",
        "viewer.continue_confirm": (
            "The task will re-run from \"{stage}\"; this stage and everything "
            "after it will be overwritten. Continue?"
        ),
        "viewer.subtitle_panel": "Subtitles",
        "viewer.subtitle_none": "No subtitles generated",
        "viewer.tts.ref_audio": "Reference audio",
        "viewer.tts.change_ref": "Change reference audio",
        "viewer.tts.change_ref_title": "Choose a reference audio file",
        "viewer.tts.ref_changed": "Reference audio changed: the task will synthesize from scratch next time.",
        "viewer.tts.final_audio": "Final audio",
        "viewer.tts.lang": "Language: {lang}",
        # ---- Audio player bar ----
        "player.play": "Play",
        "player.stop": "Stop",
        "player.no_audio": "No audio loaded",
        "player.file_missing": "Audio file not found",
        "player.playing_basic": "Playing (basic mode)",
    },
}

# ---------------------------------------------------------------- 当前语言

_current_language: str = DEFAULT_LANGUAGE


_legacy_migrated = False


def _config_dir() -> Path:
    """用户配置目录（Windows %LOCALAPPDATA%/MinimSubDub、
    macOS ~/Library/Application Support/MinimSubDub）。

    appauthor=False：platformdirs 在 Windows 上默认把 appname 当作
    appauthor 再拼一层目录（MinimSubDub/MinimSubDub），显式关闭。
    首次调用时把旧版双层目录里的 settings.json / tasks.json 迁移上来。
    """
    global _legacy_migrated
    root = Path(user_config_dir(APP_NAME, appauthor=False))
    if not _legacy_migrated:
        _legacy_migrated = True
        legacy = root / APP_NAME
        if legacy.is_dir():
            for name in ("settings.json", "tasks.json"):
                src, dst = legacy / name, root / name
                if src.is_file() and not dst.is_file():
                    try:
                        os.replace(src, dst)
                    except OSError as exc:
                        logger.warning("迁移旧配置 %s 失败：%s", src, exc)
            try:
                legacy.rmdir()  # 仅当已清空时删除
            except OSError:
                pass
    return root


def _settings_path() -> Path:
    """用户配置文件路径（跨平台用户配置目录）。"""
    return _config_dir() / "settings.json"


def _load_language() -> str:
    """启动时读取持久化的语言偏好，缺失或非法时回退默认语言。"""
    try:
        path = _settings_path()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            lang = data.get("language")
            if lang in SUPPORTED_LANGUAGES:
                return lang
    except (OSError, ValueError) as e:
        logger.warning("读取语言设置失败，使用默认语言 %s: %s", DEFAULT_LANGUAGE, e)
    return DEFAULT_LANGUAGE


def get_language() -> str:
    """当前界面语言代码（如 "zh-CN" / "en"）。"""
    return _current_language


def _read_settings() -> dict:
    """读取整个设置字典，损坏/缺失时返回空 dict。"""
    try:
        path = _settings_path()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError) as e:
        logger.warning("读取设置文件失败: %s", e)
    return {}


def _write_settings(updates: dict) -> None:
    """合并写入设置键（保留其它既有键）。"""
    data = _read_settings()
    data.update(updates)
    try:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning("设置保存失败: %s", e)


def get_setting(key: str, default=None):
    """读取一个持久化设置键（与界面语言同一 settings.json）。"""
    return _read_settings().get(key, default)


def set_setting(key: str, value) -> None:
    """持久化一个设置键（合并写入）。"""
    _write_settings({key: value})


def set_language(language: str) -> None:
    """切换当前界面语言并持久化；非法代码抛 ValueError。"""
    global _current_language
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"不支持的语言: {language}（可选: {sorted(SUPPORTED_LANGUAGES)}）")
    if language == _current_language:
        return
    _current_language = language
    _write_settings({"language": language})


def tr(key: str, **kwargs) -> str:
    """按当前语言取翻译；支持 {name} 形式的占位符格式化。

    查找顺序：当前语言 → zh-CN → 返回 key 本身（便于发现漏译）。
    """
    text = TRANSLATIONS.get(_current_language, {}).get(key)
    if text is None:
        text = TRANSLATIONS[DEFAULT_LANGUAGE].get(key)
    if text is None:
        logger.warning("缺少翻译 key: %s", key)
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            logger.warning("翻译占位符格式化失败: key=%s kwargs=%s", key, kwargs)
            return text
    return text


# 模块导入时加载持久化的语言偏好（在 QApplication 创建之前生效）
_current_language = _load_language()
