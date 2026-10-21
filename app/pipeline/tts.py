# -*- coding: utf-8 -*-
"""声音克隆 TTS：IndexTTS-2.5 子进程推理引擎。

IndexTTS-2.5（github.com/index-tts/index-tts，魔搭 IndexTeam/IndexTTS-2.5）
依赖 transformers 4.52，与主环境（ASR 所需 transformers 5.x）冲突，故运行
于独立 venv（.venv-tts，--system-site-packages 共享主环境 torch）的常驻
子进程中：

- 父进程（本模块）：管理 worker（app/pipeline/tts_worker.py）生命周期，
  经 stdin/stdout JSON Lines 协议逐句请求合成；worker 落盘成品 wav，
  静音裁剪与时长对齐（time-stretch）在父进程完成（soundfile/soxr/ffmpeg）；
- 子进程：加载一次 IndexTTS2 常驻服务，进程退出即释放显存；加载期间
  库日志（">> GPT weights restored..." 等）经 stderr 回流到界面日志。

时长控制：IndexTTS 原生 duration_factor 仅粗粒度语速缩放（0.5-2.0），
沿用既有方案——生成后按目标时长兜底：过长 time-stretch 压缩对齐；
过短不动语速，前后各补 (目标-实际)/2 静音对齐（慢放拉长会不自然）。
伸缩比 = 语音时长/目标时长（补齐静音不计入），超出
[STRETCH_REVIEW_LOW, STRETCH_REVIEW_HIGH] 由调用方标记 needs_review。

支持语言：中 / 英 / 日 / 西 / 阿（lang 代码 zh/en/ja/es/ar），其余语言
由 IndexTTS 内部回退到 common 语言 token。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import soundfile as sf

from app.core.config import AppConfig, sanitized_subprocess_env
from app.pipeline.base import ProgressCallback, report_progress
from app.pipeline.mix import stretch_to_duration

logger = logging.getLogger(__name__)

# 伸缩比超过该区间（需 >1.3x 压缩 / >1.43x 拉长）时标记 needs_review
STRETCH_REVIEW_HIGH = 1.3   # 生成时长 / 目标时长 上限
STRETCH_REVIEW_LOW = 0.7    # 下限
# 生成时长与目标偏差在该比例内直接使用，不做 time-stretch
STRETCH_TOLERANCE = 0.10

# 独立运行环境布局（开发态=项目根；冻结态=用户数据目录，见 AppConfig.DATA_ROOT）
TTS_VENV_DIR = AppConfig.DATA_ROOT / ".venv-tts"
INDEX_REPO_DIR = AppConfig.THIRD_PARTY_DIR / "index-tts"
# 冒烟通过哨兵：env_setup._setup_tts 仅在依赖导入冒烟实际通过后写入，
# tts_runtime_ready() 据此判定"上次冒烟确实通过"——防止冒烟失败后重跑
# 部署被 run() 的就绪门禁整体跳过、坏环境被误报就绪（实测云主机
# 2026-10-19 第二次部署 50 秒假成功，sentencepiece 导入仍崩）
TTS_SMOKE_SENTINEL = TTS_VENV_DIR / ".tts_smoke_ok"

# worker 脚本：开发态为源码文件；冻结态为随包分发的数据文件
# （Nuitka 编译后 __file__ 指向虚拟路径，需回退到可执行文件同级的分发目录）
_WORKER_NAME = "tts_worker.py"


def _worker_script() -> Path:
    """定位 tts_worker.py 脚本磁盘路径。"""
    candidate = Path(__file__).with_name(_WORKER_NAME)
    if candidate.is_file():
        return candidate
    # 冻结态：脚本随包分发在 <exe 目录>/app/pipeline/ 下
    import sys

    frozen_candidate = (
        Path(sys.executable).resolve().parent / "app" / "pipeline" / _WORKER_NAME
    )
    if frozen_candidate.is_file():
        return frozen_candidate
    raise FileNotFoundError(
        f"找不到 tts_worker.py（尝试过 {candidate} 与 {frozen_candidate}）"
    )

# 子进程单句合成最长等待（秒）：IndexTTS 长句低显存分片可能较慢
_SYNTH_TIMEOUT_SEC = 1800.0


class TTSLoadMemoryError(RuntimeError):
    """内存/显存不足导致 TTS 加载或合成失败（换 CPU 重试只会更糟，直接失败）。"""


# ---------------------------------------------------------------- 环境检测

def get_tts_venv_python() -> Path:
    """返回 .venv-tts 的 Python 可执行文件路径。"""
    if os.name == "nt":
        return TTS_VENV_DIR / "Scripts" / "python.exe"
    return TTS_VENV_DIR / "bin" / "python"


def _shared_pkg_exists(site_packages: Path, name: str) -> bool:
    """包目录在本地 site-packages 或 .pth 共享目录（.venv-ml 等）存在。

    pip 经共享目录看到同版本包会 "already satisfied" 跳过本地安装
    （与下方 torchaudio 检查同因，实测见 deploy 日志 2026-10-18）。
    """
    if (site_packages / name).is_dir():
        return True
    try:
        share = Path(
            (site_packages / "_share_main_venv.pth")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return False
    return (share / name).is_dir()


def tts_runtime_ready() -> tuple[bool, str]:
    """检查 TTS 独立运行环境是否齐备，返回 (就绪, 缺失说明中文)。"""
    py = get_tts_venv_python()
    if not py.is_file():
        return False, "缺少独立运行环境 .venv-tts（未创建或未完成依赖安装）"
    if not (INDEX_REPO_DIR / "indextts").is_dir():
        return False, "缺少 IndexTTS 源码 third_party/index-tts（需 git clone 官方仓库）"
    if os.name == "nt":
        site_packages = TTS_VENV_DIR / "Lib" / "site-packages"
    else:
        site_packages = TTS_VENV_DIR / "lib"
        site_packages = next(
            (p for p in site_packages.glob("python*/site-packages") if p.is_dir()),
            site_packages,
        )
    if not (site_packages / "transformers").is_dir():
        return False, ".venv-tts 内依赖未安装（transformers 4.52 等）"
    # omegaconf：vendored indextts（infer_v2_5.py）的顶层 import 依赖
    # （deploy 日志 2026-10-19 两机 TTS 模型加载报 No module named
    # 'omegaconf'）；判就绪必须可见，老部署由此触发重新部署补齐
    if not _shared_pkg_exists(site_packages, "omegaconf"):
        return False, ".venv-tts 内缺少 omegaconf（IndexTTS 顶层依赖，重新部署可补齐）"
    # 源码补丁标记：老部署的上游 infer_v2_5.py 未打 modelscope 可选化
    # 补丁（部署源码来自 GitHub zip 快照，项目内补丁副本不会到用户
    # 机），TTS 模型加载会 ModuleNotFoundError: modelscope——判未就绪
    # 以触发重新部署（env_setup._patch_indextts_modelscope 幂等补齐）
    try:
        infer_src = (INDEX_REPO_DIR / "indextts" / "infer_v2_5.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False, "无法读取 IndexTTS 源码 infer_v2_5.py（需重新部署）"
    if "ESubDub 部署适配" not in infer_src:
        return False, "IndexTTS 源码未打部署补丁（重新部署可补齐）"
    # model_download.py 的加载期兜底补丁：首次加载缺辅助模型时上游会
    # 现场联网下载，国内网络首选 modelscope——未打补丁的旧部署在此
    # ModuleNotFoundError（实测 2026-10-19 独显机），同样判未就绪触发
    # 重新部署补齐
    try:
        md_src = (
            INDEX_REPO_DIR / "indextts" / "utils" / "model_download.py"
        ).read_text(encoding="utf-8")
    except OSError:
        return False, "无法读取 IndexTTS 源码 model_download.py（需重新部署）"
    if "ESubDub 部署适配" not in md_src:
        return False, "IndexTTS 源码未打加载期补丁（重新部署可补齐）"
    ta_dir = site_packages / "torchaudio"
    if not ta_dir.is_dir():
        # pip 经 .pth 共享目录（.venv-ml / 主环境）看到同版本 torchaudio
        # 时会 "already satisfied" 跳过本地安装（共享侧装完同样剥离了
        # C 扩展，worker import 与本地副本完全等价，实测见 deploy 日志
        # 2026-10-18）：改以共享目录为检查对象
        pth = site_packages / "_share_main_venv.pth"
        try:
            share = Path(pth.read_text(encoding="utf-8").strip())
            ta_dir = share / "torchaudio"
        except OSError:
            pass
    if not ta_dir.is_dir():
        return False, (
            ".venv-tts 内缺少 torchaudio 2.8"
            "（pip install --no-deps torchaudio==2.8.0 后删除 torchaudio/lib 下的 C 扩展）"
        )
    for ext in ("_torchaudio.pyd", "_torchaudio.so"):
        if (ta_dir / "lib" / ext).is_file():
            return False, (
                "torchaudio 的 C 扩展依赖 venv 内未装的配套二进制，"
                "请删除 torchaudio/lib 下的 _torchaudio.* 与 libtorchaudio.*"
            )
    # 冒烟通过哨兵（TTS_SMOKE_SENTINEL 定义处注释）：缺失说明本机
    # 从未通过导入冒烟、或上次冒烟失败后环境有变——判未就绪以触发
    # 重新部署重跑冒烟，避免坏环境潜伏到任务执行期才爆
    if not TTS_SMOKE_SENTINEL.is_file():
        return False, "TTS 环境未通过导入冒烟验证（重新部署可补齐）"
    return True, ""


def _trim_silence(wav, threshold_db: float = -45.0) -> "object":
    """去掉首尾近静音（按帧 RMS 阈值），全静音时原样返回。

    使用 30ms 帧长扫描，从首/尾找到第一个超阈帧，向前/向后多保留 60ms 余量，
    保证起音自然。
    """
    import numpy as np

    if wav is None or len(wav) == 0:
        return wav
    data = np.asarray(wav, dtype=np.float32)
    frame = max(1, int(0.03 * 24000))  # 帧长按采样率无关的近似值扫描
    n = len(data) // frame
    if n < 3:
        return data
    frames = data[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    thr = 10.0 ** (threshold_db / 20.0)
    nonzero = np.nonzero(rms > thr)[0]
    if len(nonzero) == 0:
        return data
    pad_frames = 2  # 约 60ms 余量
    start = max(0, (nonzero[0] - pad_frames) * frame)
    end = min(len(data), (nonzero[-1] + 1 + pad_frames) * frame)
    return data[start:end]


class TTSEngine:
    """IndexTTS-2.5 推理引擎：启动 / 请求 / 释放 worker 子进程。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._stdout_queue: queue.Queue = queue.Queue()
        self._stderr_lines: deque = deque()
        self._stderr_tail: deque = deque(maxlen=30)  # 报错时取末尾若干行
        self._next_id = 1
        # 最近单句合成的 (目标时长, 墙钟耗时)：供后续合成按历史耗时
        # 插值单句进度百分比（复核窗口即时重生成时显示）
        self._synth_stats: "list[tuple[float, float]]" = []

    @property
    def loaded(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ 加载

    def load(
        self,
        model_dir: str,
        device: str = "cpu",
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        """启动 worker 子进程并等待模型加载完成（ready 消息）。

        加载期间的库日志经 stderr 回流为进度消息（界面日志可见各组件
        加载次序）；kind=memory 的致命错误抛 TTSLoadMemoryError。
        """
        self.unload()

        ok, detail = tts_runtime_ready()
        if not ok:
            raise RuntimeError(f"TTS 运行环境未就绪：{detail}")
        if not (Path(model_dir) / "config.yaml").is_file():
            raise FileNotFoundError(f"IndexTTS-2.5 模型目录无效（缺 config.yaml）: {model_dir}")

        cmd = [
            str(get_tts_venv_python()), str(_worker_script()),
            "--model-dir", str(model_dir),
            "--device", device,
        ]
        if device.startswith("cuda"):
            cmd.append("--bf16")

        report_progress(progress, 0.05, f"启动 TTS 推理子进程（{device}）")
        # 净化环境：冻结态把随包 DLL 目录从 PATH 剔除，避免 worker 的
        # torch 原生扩展命中随包 DLL（WinError 1114 等）
        env = sanitized_subprocess_env()
        env.update({
            # 冻结态 third_party 不在 worker 脚本推导的项目根下，显式指路
            "ESUBDUB_INDEX_REPO": str(INDEX_REPO_DIR),
            # 冻结态 torchaudio 垫片等兜底资源的所在目录
            "ESUBDUB_DATA_ROOT": str(AppConfig.DATA_ROOT),
            # 强制 IndexTTS 辅助模型（w2v-bert/campplus/bigvgan 等）走魔搭下载：
            # 国内网络对 huggingface.co 常见 TCP 可通但 TLS 证书校验失败，
            # IndexTTS 的 need_proxy() 会误判为可直连；此开关使其改走
            # ModelScope（映射见 indextts/utils/model_download.py，失败兜底 hf-mirror）
            "USE_MODELSCOPE": "true",
        })
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(AppConfig.DATA_ROOT),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._stdout_queue = queue.Queue()
        self._stderr_lines.clear()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        report_progress(
            progress, 0.1,
            "加载 IndexTTS-2.5 权重（首次运行会自动下载辅助模型，请耐心等待）",
        )
        while True:
            msg = self._wait_message(timeout=0.25)
            if msg is None:
                # 心跳：泵出库日志并承担取消检查点（消息固定，避免日志刷屏）
                self._pump_stderr_logs(progress, frac=0.5)
                report_progress(progress, 0.5, "正在加载 IndexTTS-2.5（各组件进度见日志）")
                continue
            event = msg.get("event")
            if event == "ready":
                break
            if event == "fatal":
                error = str(msg.get("error") or "未知错误")
                if msg.get("kind") == "memory":
                    raise TTSLoadMemoryError(
                        f"内存/显存不足：加载 IndexTTS-2.5 失败（约需 6GB 显存）。{error}"
                    )
                raise RuntimeError(f"加载 IndexTTS-2.5 失败：{error}")
            # 其余消息（理论上不会有）忽略继续等
        self._pump_stderr_logs(progress, frac=0.98)
        report_progress(progress, 1.0, "IndexTTS-2.5 加载完成")

    def unload(self) -> None:
        """结束 worker 子进程（先礼后兵），释放显存。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write('{"cmd": "shutdown"}\n')  # type: ignore[union-attr]
                    proc.stdin.flush()  # type: ignore[union-attr]
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------ 合成

    def synthesize(
        self,
        text: str,
        ref_wav: str,
        target_duration: float,
        out_path: str,
        lang: str = "zh",
        progress: Optional[ProgressCallback] = None,
        emo_vector: Optional[list] = None,
        duration_factor: float = 1.0,
    ) -> tuple[float, float]:
        """克隆参考音色合成一句语音，返回 (实际时长, 伸缩比)。

        - worker 完成推理并落盘；父进程裁首尾静音后按目标时长判断：
          偏差在 STRETCH_TOLERANCE 外——过长 time-stretch 压缩对齐，
          过短前后各补 (目标-实际)/2 静音对齐（不做慢速拉长）；
        - stretch_ratio = 裁剪后语音时长 / 目标时长（不含补齐的静音），
          超出 [0.7, 1.3] 由调用方标记 needs_review；
        - target_duration=0 为"自由时长"模式（文本转语音任务）：只裁
          首尾静音、不做任何对齐，伸缩比恒为 1.0，时长交给模型自然生成；
        - emo_vector：8 维情绪权重向量 [happy, angry, sad, afraid,
          disgusted, melancholic, surprised, calm]，None = 跟随参考音频；
        - duration_factor：IndexTTS 原生时长系数（1.0 正常；<1 更快、
          >1 更慢，即语速的倒数），仅自由时长模式传入；
        - progress：单句合成进度回调。worker 转发 IndexTTS 的阶段锚点
          （0 起步 / 0.1 文本处理 / 0.2+0.7·i/n 逐段生成 / 0.9 落盘），
          锚点之间按最近历史合成耗时线性插值（0.2→0.9 主段平滑推进）。
        """
        if not self.loaded:
            raise RuntimeError("TTS 模型未加载，请先调用 load()")
        if not Path(ref_wav).is_file():
            raise FileNotFoundError(f"参考音频不存在: {ref_wav}")

        rid = self._next_id
        self._next_id += 1
        free_mode = target_duration <= 0
        request = {
            "id": rid, "text": text, "ref": str(ref_wav),
            "out": str(out_path), "lang": lang,
            "duration_factor": float(duration_factor),
        }
        if emo_vector is not None:
            request["emo_vector"] = [float(v) for v in emo_vector]
        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

        t_start = time.perf_counter()
        expected = self._estimate_synth_seconds(
            target_duration, len(text), duration_factor,
        )
        gen_started_at: float | None = None  # 收到 ≥0.2 锚点（进入 AR 生成主段）的时刻
        last_frac = 0.0

        def _report(frac: float) -> None:
            """单调上报单句进度（完成前夹在 [0, 0.95]，避免假满格）。"""
            nonlocal last_frac
            frac = min(max(frac, 0.0), 0.95)
            if frac > last_frac:
                last_frac = frac
                report_progress(progress, frac, "TTS 合成")

        waited = 0.0
        while True:
            msg = self._wait_message(timeout=0.5)
            if msg is None:
                waited += 0.5
                self._pump_stderr_logs(None)
                if waited > _SYNTH_TIMEOUT_SEC:
                    raise RuntimeError(f"TTS 合成超时（>{_SYNTH_TIMEOUT_SEC:.0f} 秒）")
                if progress is not None:
                    if gen_started_at is not None:
                        # 生成主段（约占总耗时 70%）：按历史耗时线性插值
                        run = (time.perf_counter() - gen_started_at) / expected
                        _report(0.2 + 0.7 * min(run, 1.0))
                    else:
                        _report(0.05)  # 尚未收到首个锚点
                continue
            if msg.get("event") == "progress":
                # worker 转发的 IndexTTS 阶段锚点
                frac = float(msg.get("frac") or 0.0)
                if gen_started_at is None and frac >= 0.2:
                    gen_started_at = time.perf_counter()
                _report(frac)
                continue
            if msg.get("id") != rid:
                continue  # 迟到的其他响应（如上句失败后的残留），忽略
            if not msg.get("ok"):
                error = str(msg.get("error") or "未知错误")
                if msg.get("kind") == "memory":
                    raise TTSLoadMemoryError(f"内存/显存不足：TTS 合成失败。{error}")
                raise RuntimeError(f"TTS 合成失败：{error}")
            break

        # 记录本句耗时（目标时长, 墙钟秒）供后续进度插值估时
        self._synth_stats.append((target_duration, time.perf_counter() - t_start))
        if len(self._synth_stats) > 30:
            del self._synth_stats[:-30]

        # 后处理（父进程）：裁静音；配音任务再做时长对齐
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        wav, sr = sf.read(str(out_path), dtype="float32", always_2d=True)
        trimmed = _trim_silence(wav[:, 0])
        sf.write(str(out_path), trimmed, sr, subtype="PCM_16")
        raw_duration = len(trimmed) / sr

        if free_mode:
            # 自由时长（文本转语音）：保留模型自然生成时长，伸缩比恒 1.0
            return raw_duration, 1.0

        # 伸缩比只计语音本身的时长；补齐的静音不计入
        ratio = raw_duration / target_duration if target_duration > 0 else 1.0
        if abs(raw_duration - target_duration) / max(target_duration, 1e-6) > STRETCH_TOLERANCE:
            if raw_duration > target_duration:
                # 过长：time-stretch 压缩到目标时长（保持与原句对齐）
                stretched = stretch_to_duration(str(out_path), target_duration, str(out_path))
                logger.info(
                    "TTS 时长对齐: 原始 %.2fs → 目标 %.2fs（伸缩比 %.2f，压缩后 %.2fs）",
                    raw_duration, target_duration, ratio, stretched,
                )
            else:
                # 过短：前后各补 (目标-实际)/2 静音到目标时长；
                # 不做慢速拉长（time-stretch 放慢语速会导致说话不自然）
                import numpy as np

                pad = int(round((target_duration - raw_duration) / 2 * sr))
                sf.write(str(out_path), np.pad(trimmed, (pad, pad)), sr, subtype="PCM_16")
                logger.info(
                    "TTS 时长补齐: 原始 %.2fs → 目标 %.2fs（伸缩比 %.2f，前后各补 %.2fs 静音）",
                    raw_duration, target_duration, ratio, pad / sr,
                )
        return raw_duration, ratio

    def _estimate_synth_seconds(
        self, target_duration: float, text_len: int = 0,
        duration_factor: float = 1.0,
    ) -> float:
        """按最近合成耗时估算单句墙钟耗时（进度插值用）。

        用最近 30 句中最大的 墙钟/目标时长 比再放宽 25%，保证进度条
        宁慢勿快（过早到 90% 后停在落盘等待比慢慢涨更糟）；
        自由时长模式（目标时长未知）先按文本长度估音频时长
        （约 0.2 秒/字符，除以时长系数修正语速），再走同样的历史比例。
        """
        est_duration = target_duration
        if est_duration <= 0:
            est_duration = max(text_len * 0.2 / max(duration_factor, 0.1), 1.0)
        if self._synth_stats:
            ratio = max(w / max(d, 0.1) for d, w in self._synth_stats)
            return max(est_duration * ratio * 1.25, 1.0)
        return max(est_duration * 2.0, 10.0)

    # ------------------------------------------------------------------ 子进程 IO

    def _drain_stdout(self) -> None:
        """后台线程：stdout → 消息队列；EOF 放入空串哨兵。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if line:
                self._stdout_queue.put(line)
        self._stdout_queue.put("")  # EOF 哨兵（协议消息均为非空行）

    def _drain_stderr(self) -> None:
        """后台线程：stderr → 日志行缓冲（供泵出到界面/日志）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, ""):
            self._stderr_lines.append(line)

    def _wait_message(self, timeout: float) -> Optional[dict]:
        """取下一条协议消息；超时返回 None；子进程退出抛 RuntimeError。"""
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"TTS 子进程意外退出（code={proc.poll()}）：{self._stderr_text()}"
                )
            return None
        if line == "":
            raise RuntimeError(f"TTS 子进程已退出：{self._stderr_text()}")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("TTS worker 非协议消息: %s", line[:200])
            return None

    def _pump_stderr_logs(
        self,
        progress: Optional[ProgressCallback],
        frac: float = 0.5,
    ) -> None:
        """把 worker 的 stderr 日志行泵出到进度回调（或本地日志）。

        加载期传 progress → 界面日志可见各组件加载次序；合成期传 None →
        仅记入本地日志。IndexTTS 用 ">>" 前缀打印关键节点，过滤保留。
        """
        while True:
            try:
                line = self._stderr_lines.popleft()
            except IndexError:
                break
            line = line.strip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if progress is not None:
                report_progress(progress, frac, line)
            else:
                logger.info("[tts-worker] %s", line)

    def _stderr_text(self) -> str:
        """报错用的 stderr 末尾若干行（截断到单行 200 字符）。"""
        lines = [ln[:200] for ln in self._stderr_tail]
        return " | ".join(lines[-8:]) or "（无输出）"
