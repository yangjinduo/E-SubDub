# -*- coding: utf-8 -*-
"""声音克隆 TTS：IndexTTS-2.5 子进程推理引擎。

IndexTTS-2.5（github.com/index-tts/index-tts，魔搭 IndexTeam/IndexTTS-2.5）
依赖 transformers 4.52，与主环境（ASR 所需 transformers 5.x）冲突，故运行
于独立 venv（.venv-tts，--system-site-packages 共享主环境 torch）的常驻
子进程中：

- 父进程（本模块）：管理 worker（app/pipeline/tts_worker.py）生命周期，
  经 stdin/stdout JSON Lines 协议逐句请求合成；worker 落盘成品 wav，
  静音裁剪与时长对齐（time-stretch）在父进程完成（soundfile/librosa）；
- 子进程：加载一次 IndexTTS2 常驻服务，进程退出即释放显存；加载期间
  库日志（">> GPT weights restored..." 等）经 stderr 回流到界面日志。

时长控制：IndexTTS 原生 duration_factor 仅粗粒度语速缩放（0.5-2.0），
沿用既有方案——生成后按目标时长 time-stretch 兜底，伸缩比超出
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
from collections import deque
from pathlib import Path
from typing import Optional

import soundfile as sf

from app.core.config import AppConfig
from app.pipeline.base import ProgressCallback, report_progress
from app.pipeline.mix import stretch_to_duration

logger = logging.getLogger(__name__)

# 伸缩比超过该区间（需 >1.3x 压缩 / >1.43x 拉长）时标记 needs_review
STRETCH_REVIEW_HIGH = 1.3   # 生成时长 / 目标时长 上限
STRETCH_REVIEW_LOW = 0.7    # 下限
# 生成时长与目标偏差在该比例内直接使用，不做 time-stretch
STRETCH_TOLERANCE = 0.10

# 独立运行环境布局（项目根相对路径）
TTS_VENV_DIR = AppConfig.APP_ROOT / ".venv-tts"
INDEX_REPO_DIR = AppConfig.APP_ROOT / "third_party" / "index-tts"
_WORKER_PATH = Path(__file__).with_name("tts_worker.py")

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
    ta_dir = site_packages / "torchaudio"
    if not ta_dir.is_dir():
        return False, (
            ".venv-tts 内缺少 torchaudio 2.8"
            "（pip install --no-deps torchaudio==2.8.0 后删除 torchaudio/lib 下的 C 扩展）"
        )
    for ext in ("_torchaudio.pyd", "_torchaudio.so"):
        if (ta_dir / "lib" / ext).is_file():
            return False, (
                "torchaudio 的 C 扩展与共享的 torch 2.14 二进制不兼容，"
                "请删除 .venv-tts 内 torchaudio/lib 下的 _torchaudio.* 与 libtorchaudio.*"
            )
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
            str(get_tts_venv_python()), str(_WORKER_PATH),
            "--model-dir", str(model_dir),
            "--device", device,
        ]
        if device.startswith("cuda"):
            cmd.append("--bf16")

        report_progress(progress, 0.05, f"启动 TTS 推理子进程（{device}）")
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            # 强制 IndexTTS 辅助模型（w2v-bert/campplus/bigvgan 等）走魔搭下载：
            # 国内网络对 huggingface.co 常见 TCP 可通但 TLS 证书校验失败，
            # IndexTTS 的 need_proxy() 会误判为可直连；此开关使其改走
            # ModelScope（映射见 indextts/utils/model_download.py，失败兜底 hf-mirror）
            "USE_MODELSCOPE": "true",
        }
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(AppConfig.APP_ROOT),
            env=env,
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
    ) -> tuple[float, float]:
        """克隆参考音色合成一句语音，返回 (实际时长, 伸缩比)。

        - worker 完成推理并落盘；父进程裁首尾静音后按目标时长判断：
          偏差在 STRETCH_TOLERANCE 外用 time-stretch 对齐；
        - stretch_ratio = 裁剪后时长 / 目标时长，超出 [0.7, 1.3] 由调用方
          标记 needs_review。
        """
        if not self.loaded:
            raise RuntimeError("TTS 模型未加载，请先调用 load()")
        if not Path(ref_wav).is_file():
            raise FileNotFoundError(f"参考音频不存在: {ref_wav}")

        rid = self._next_id
        self._next_id += 1
        request = {
            "id": rid, "text": text, "ref": str(ref_wav),
            "out": str(out_path), "lang": lang,
        }
        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        self._proc.stdin.flush()  # type: ignore[union-attr]

        waited = 0.0
        while True:
            msg = self._wait_message(timeout=0.5)
            if msg is None:
                waited += 0.5
                self._pump_stderr_logs(None)
                if waited > _SYNTH_TIMEOUT_SEC:
                    raise RuntimeError(f"TTS 合成超时（>{_SYNTH_TIMEOUT_SEC:.0f} 秒）")
                continue
            if msg.get("id") != rid:
                continue  # 迟到的其他响应（如上句失败后的残留），忽略
            if not msg.get("ok"):
                error = str(msg.get("error") or "未知错误")
                if msg.get("kind") == "memory":
                    raise TTSLoadMemoryError(f"内存/显存不足：TTS 合成失败。{error}")
                raise RuntimeError(f"TTS 合成失败：{error}")
            break

        # 后处理（父进程）：裁静音 + 时长对齐
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        wav, sr = sf.read(str(out_path), dtype="float32", always_2d=True)
        trimmed = _trim_silence(wav[:, 0])
        sf.write(str(out_path), trimmed, sr, subtype="PCM_16")
        raw_duration = len(trimmed) / sr

        ratio = raw_duration / target_duration if target_duration > 0 else 1.0
        if abs(raw_duration - target_duration) / max(target_duration, 1e-6) > STRETCH_TOLERANCE:
            stretched = stretch_to_duration(str(out_path), target_duration, str(out_path))
            logger.info(
                "TTS 时长对齐: 原始 %.2fs → 目标 %.2fs（伸缩比 %.2f，拉伸后 %.2fs）",
                raw_duration, target_duration, ratio, stretched,
            )
        return raw_duration, ratio

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
