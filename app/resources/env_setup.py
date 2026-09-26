# -*- coding: utf-8 -*-
"""运行环境搭建：TTS 独立虚拟环境（.venv-tts）+ llama-server 可执行文件。

设置窗口"运行环境"行 / 资源栏推理设备卡对应的后台逻辑：
- TTS 环境：检测复用 app.pipeline.tts.tts_runtime_ready()；搭建步骤复刻
  已手工验证的流程——venv 以 --system-site-packages 创建并写 .pth 共享
  主环境 torch（避免重复安装 3GB CUDA torch）、IndexTTS 源码克隆到
  third_party/index-tts、核心依赖 --no-deps 安装（防止 pip 把共享 torch
  降级）、torchaudio 2.8 装完后剥离其 C 扩展（与共享 torch 2.14 二进制
  不兼容，纯 Python 层完整可用）。
- LLAMA 服务：llama.cpp 预编译版只在 GitHub Releases 发布，经 gh-proxy.com
  加速下载（直连兜底），按平台/设备选包（Windows：有 N 卡选 CUDA 构建，
  否则 CPU 构建；macOS：arm64 / x64），解压到 tools/llama.cpp/。CUDA 构建
  需一并下载 cudart 伴随包（cublas/cudart 运行库，ggml-cuda.dll 动态链接
  它们；缺失时 llama-server 静默回退 CPU）。

所有步骤幂等：已完成部分（venv 齐备、源码在位、llama-server 在位）自动
跳过，失败或取消后重跑只补缺失部分。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
import sysconfig
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, Signal

from app.core.config import AppConfig
from app.core.hardware import DeviceInfo
from app.core.i18n import tr
from app.pipeline.llm_engine import resolve_llama_server_exe
from app.pipeline.tts import (
    INDEX_REPO_DIR,
    TTS_VENV_DIR,
    get_tts_venv_python,
    tts_runtime_ready,
)

logger = logging.getLogger(__name__)

# GitHub 加速代理（前缀 + 完整 URL）
GH_PROXY = "https://gh-proxy.com/"

INDEXTTS_REPO = "https://github.com/index-tts/index-tts.git"
LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
LLAMA_TOOLS_DIR = AppConfig.APP_ROOT / "tools" / "llama.cpp"

# ---------------------------------------------------------------- 依赖清单
# 版本与手工验证通过的现场环境完全一致（.venv-tts 逐一核对 dist-info）

# 核心：必须 --no-deps 安装，否则 pip 会把共享的 torch 2.14 解析降级到 2.8
_NO_DEPS_CORE = [
    "transformers==4.52.1",
    "tokenizers==0.21.0",
    "huggingface-hub==0.36.2",
    "torchaudio==2.8.0",
    "torch-stoi==0.2.3",
    "descript-audiotools==0.7.2",
    "openai-whisper==20250625",
]

# IndexTTS 运行依赖：--no-deps 安装（其声明依赖 torch/numba/keras 等要么由
# 主环境共享提供，要么运行时并不需要）
_NO_DEPS_RUNTIME = [
    "cn2an==0.5.24",
    "fugashi==1.5.2",
    "unidic-lite==1.0.8",
    "g2p-en==2.1.0",
    "jieba==0.42.1",
    "json5==0.15.0",
    "munch==4.0.0",
    "sentencepiece==0.2.2",
    "textstat==0.7.13",
    "wetext==0.1.8",
    "kaldifst==1.8.0",
    "ffmpeg-python==0.2.0",
]

# 子依赖：带依赖安装安全（pip 能看到共享的 torch/numpy，不会重复安装；
# 纯 Python 包，不触碰共享环境）
_SUB_DEPS = [
    "matplotlib==3.11.2",
    "argbind==0.3.9",
    "ffmpy==1.0.0",
    "flatten-dict==0.5.0",
    "importlib-resources==7.1.0",
    "ipython==9.17.1",
    "markdown2==2.5.5",
    "pystoi==0.4.1",
    "randomname==0.2.1",
    "tensorboard==2.21.0",
    "tiktoken==0.14.0",
    "nltk==3.10.3",
    "distance==0.1.3",
    "textsearch==0.0.24",
    "anyascii==0.3.3",
    "pyahocorasick==2.3.1",
    "pyphen==0.18.1",
    "contractions==0.1.73",
    "more-itertools==11.1.0",
]

# torchaudio 2.8 的 C 扩展按 torch 2.8 编译，与共享 torch 2.14 二进制不兼容
# （WinError 127）；剥离后 import 探测返回 False，纯 Python 层完整工作
_TORCHAUDIO_C_EXTS = [
    "_torchaudio.pyd",
    "_torchaudio.so",
    "libtorchaudio.pyd",
    "libtorchaudio.so",
]

# .venv-tts 内执行的导入冒烟测试（覆盖全部 --no-deps 安装的包）
_SMOKE_IMPORTS = (
    "import transformers, torchaudio, matplotlib, audiotools, torch_stoi, "
    "json5, wetext, g2p_en, cn2an, jieba, whisper, sentencepiece, fugashi, "
    "unidic_lite, textstat, ffmpeg, munch, argbind, pystoi, randomname, "
    "nltk, tiktoken"
)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class _Cancelled(Exception):
    """用户主动取消。"""


# ---------------------------------------------------------------- 状态查询

def environment_status() -> dict:
    """当前运行环境状态：TTS 虚拟环境 + llama-server（均只做文件级检查，毫秒级）。"""
    tts_ok, tts_detail = tts_runtime_ready()
    return {
        "tts_ready": tts_ok,
        "tts_detail": tts_detail,
        "llama_ready": _llama_runtime_ready(),
    }


def _llama_runtime_ready() -> bool:
    """llama-server 就绪 = 可执行存在 + CUDA 构建的运行库（cublas）齐全。

    ggml-cuda.dll 动态链接 cublas64_XX.dll / cudart64_XX.dll（官方 cudart
    伴随包提供）；缺失时 llama-server 能启动但 GPU offload 不生效、全程
    CPU，视为未就绪，以便环境搭建自动补齐伴随包。
    """
    exe = resolve_llama_server_exe()
    if not exe:
        return False
    if os.name == "nt":
        dll_dir = Path(exe).parent
        if (dll_dir / "ggml-cuda.dll").is_file():
            for name in os.listdir(dll_dir):
                if re.fullmatch(r"cublas64_\d+\.dll", name):
                    return True
            return False
    return True


def _main_site_packages() -> Path:
    """主环境（当前解释器）site-packages 绝对路径。"""
    return Path(sysconfig.get_paths()["purelib"])


def _venv_site_packages() -> Path:
    """定位 .venv-tts 的 site-packages 目录（跨平台）。"""
    if os.name == "nt":
        return TTS_VENV_DIR / "Lib" / "site-packages"
    lib = TTS_VENV_DIR / "lib"
    for p in lib.glob("python*/site-packages"):
        if p.is_dir():
            return p
    return lib / "site-packages"


# ---------------------------------------------------------------- 搭建线程

class _EnvSetupThread(QThread):
    """执行环境搭建的后台线程（步骤进度 / 子进程日志 / 取消）。

    信号：
        step_changed(str): 当前步骤文案（含下载百分比与速度）
        detail_line(str): 子进程输出行（pip / git 实时日志）
        done(dict): {"tts_ready": bool, "llama_ready": bool}
        failed(str): 错误说明
        cancelled(): 用户主动取消
    """

    step_changed = Signal(str)
    detail_line = Signal(str)
    done = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, device: DeviceInfo | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._device = device
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None

    def cancel(self) -> None:
        """请求取消：终止当前子进程，线程在最近的检查点退出。"""
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------------ 线程体

    def run(self) -> None:
        try:
            results: dict = {}

            # ---- TTS 虚拟环境 ----
            self.step_changed.emit(tr("env.step.check_tts"))
            tts_ok, _ = tts_runtime_ready()
            if not tts_ok:
                self._setup_tts()
                tts_ok, detail = tts_runtime_ready()
                if not tts_ok:
                    raise RuntimeError(f"TTS 环境搭建后仍未通过检查：{detail}")
            results["tts_ready"] = tts_ok

            # ---- llama-server ----
            if not _llama_runtime_ready():
                self._setup_llama()
            results["llama_ready"] = _llama_runtime_ready()

            self.done.emit(results)
        except _Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - 线程边界统一上报
            logger.exception("环境搭建失败")
            self.failed.emit(f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ TTS

    def _setup_tts(self) -> None:
        """搭建 .venv-tts（全部步骤幂等，已存在的自动跳过）。"""
        # 1. 创建虚拟环境（--system-site-packages 使基础解释器的标准库扩展可见）
        if not get_tts_venv_python().is_file():
            self.step_changed.emit(tr("env.step.venv"))
            self._run([sys.executable, "-m", "venv", "--system-site-packages", str(TTS_VENV_DIR)])

        # 2. 写 .pth 共享主环境 site-packages（torch 2.14 / numpy 等免重复安装）
        self.step_changed.emit(tr("env.step.pth"))
        site_packages = _venv_site_packages()
        site_packages.mkdir(parents=True, exist_ok=True)
        pth = site_packages / "_share_main_venv.pth"
        if not pth.is_file():
            pth.write_text(str(_main_site_packages()), encoding="utf-8")

        # 3. 克隆 IndexTTS 源码（gh-proxy 优先，直连兜底）
        if not (INDEX_REPO_DIR / "indextts").is_dir():
            self.step_changed.emit(tr("env.step.clone"))
            last_err: Exception | None = None
            for url in (GH_PROXY + INDEXTTS_REPO, INDEXTTS_REPO):
                try:
                    self._run(["git", "clone", "--depth", "1", url, str(INDEX_REPO_DIR)])
                    last_err = None
                    break
                except RuntimeError as e:
                    last_err = e
            if last_err is not None:
                raise RuntimeError(f"克隆 IndexTTS 源码失败（代理与直连均失败）：{last_err}")

        # 4-6. 依赖安装
        self._pip_install(_NO_DEPS_CORE, tr("env.step.pip_core"), no_deps=True)
        self._pip_install(_NO_DEPS_RUNTIME, tr("env.step.pip_runtime"), no_deps=True)
        self._pip_install(_SUB_DEPS, tr("env.step.pip_sub"), no_deps=False)

        # 7. 剥离 torchaudio C 扩展
        self.step_changed.emit(tr("env.step.strip"))
        ta_lib = _venv_site_packages() / "torchaudio" / "lib"
        for name in _TORCHAUDIO_C_EXTS:
            ext = ta_lib / name
            if ext.is_file():
                ext.unlink()

        # 8. 导入冒烟测试（在 .venv-tts 解释器内执行）
        self.step_changed.emit(tr("env.step.verify"))
        proc = subprocess.run(
            [str(get_tts_venv_python()), "-c", _SMOKE_IMPORTS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            cwd=str(AppConfig.APP_ROOT),
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise RuntimeError(f"TTS 依赖导入验证失败：\n{tail}")

    # ------------------------------------------------------------------ LLAMA

    def _setup_llama(self) -> None:
        """下载并解压 llama.cpp 预编译 llama-server 到 tools/llama.cpp/。

        Windows CUDA 构建的 ggml-cuda.dll 动态链接 cublas/cudart 运行库
        （官方 cudart 伴随包提供，主包不含）；缺失时 llama-server 能启动
        但 -ngl 不生效、静默回退 CPU。因此 CUDA 设备额外下载伴随包；
        已有 llama-server 而仅缺运行库时只补伴随包，不重下主包。
        """
        patterns = self._llama_asset_patterns()
        if not patterns:
            raise RuntimeError(
                "当前平台不支持自动下载 llama.cpp（Linux 请通过包管理器安装 llama-server）"
            )

        self.step_changed.emit(tr("env.step.llama_api"))
        release, asset = self._fetch_release(patterns)

        tools_dir = LLAMA_TOOLS_DIR.parent
        tools_dir.mkdir(parents=True, exist_ok=True)

        if not resolve_llama_server_exe():
            self._download_and_extract_asset(asset, tools_dir)
            self._flatten_llama_dir()

        # CUDA 构建：补齐 cublas/cudart 运行库（cudart 伴随包）
        cuda_major = self._installed_cuda_major() or self._asset_cuda_major(asset)
        if cuda_major and not _llama_runtime_ready():
            cudart = self._cudart_asset_for(release, cuda_major)
            if cudart is not None:
                self._download_and_extract_asset(cudart, tools_dir)

        if not resolve_llama_server_exe():
            raise RuntimeError("llama-server 已解压但未被识别，请检查 tools/llama.cpp 目录")

    def _download_and_extract_asset(self, asset: dict, tools_dir: Path) -> None:
        """下载发布资产（gh-proxy 优先，直连兜底）并解压到 tools/llama.cpp/。"""
        tmp = tools_dir / asset["name"]
        try:
            self._download_file(asset["browser_download_url"], tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        self.step_changed.emit(tr("env.step.llama_extract"))
        LLAMA_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        if tmp.name.endswith(".zip"):
            with zipfile.ZipFile(tmp) as zf:
                zf.extractall(LLAMA_TOOLS_DIR)
        else:
            with tarfile.open(tmp, "r:gz") as tf:
                tf.extractall(LLAMA_TOOLS_DIR, filter="data")
        tmp.unlink(missing_ok=True)

    def _flatten_llama_dir(self) -> None:
        """部分平台的压缩包带子目录：定位 llama-server 并把同目录文件摊平到根。"""
        exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
        if (LLAMA_TOOLS_DIR / exe_name).is_file():
            return
        found = list(LLAMA_TOOLS_DIR.rglob(exe_name))
        if not found:
            raise RuntimeError("llama-server 解压后未找到可执行文件")
        src_dir = found[0].parent
        for f in src_dir.iterdir():
            if f.is_file():
                dest = LLAMA_TOOLS_DIR / f.name
                if not dest.exists():
                    dest.write_bytes(f.read_bytes())

    @staticmethod
    def _asset_cuda_major(asset: dict) -> "str | None":
        """主资产名中的 CUDA 大版本（llama-*-bin-win-cuda-12.4-x64.zip → "12"）。"""
        m = re.search(r"-bin-win-cuda-(\d+)\.", str(asset.get("name", "")))
        return m.group(1) if m else None

    @staticmethod
    def _installed_cuda_major() -> "str | None":
        """已安装 ggml-cuda.dll 依赖的 cublas 大版本（mmap 扫描导入的 DLL 名）。

        导入表字符串位于 .rdata 段（该 DLL 的 .text 段占 500MB+，字符串
        离文件头很远），需全文件扫描；仅在环境搭建时执行一次。
        """
        exe = resolve_llama_server_exe()
        if not exe:
            return None
        dll = Path(exe).with_name("ggml-cuda.dll")
        if not dll.is_file():
            return None
        import mmap

        pattern = re.compile(rb"cublas64_(\d+)\.dll")
        try:
            with open(dll, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    m = pattern.search(mm)
                    return m.group(1).decode() if m else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _cudart_asset_for(release: dict, cuda_major: str) -> "dict | None":
        """同发布中匹配 CUDA 大版本的 cudart 伴随包（cudart-llama-*-win-cuda-12.4-x64.zip）。"""
        for asset in release.get("assets", []):
            name = str(asset.get("name", ""))
            if (
                name.startswith("cudart-llama")
                and "-win-" in name
                and f"-cuda-{cuda_major}." in name
                and name.endswith(".zip")
            ):
                return asset
        return None

    def _llama_asset_patterns(self) -> list[str]:
        """按平台/设备给出发布资产文件名后缀的优先级列表。"""
        if sys.platform == "win32":
            has_cuda = self._device is not None and self._device.device == "cuda"
            if has_cuda:
                return [
                    "-bin-win-cuda-12.4-x64.zip",   # 驱动兼容面最广
                    "-bin-win-cuda-13.4-x64.zip",
                    "-bin-win-cpu-x64.zip",
                ]
            return ["-bin-win-cpu-x64.zip", "-bin-win-cpu-arm64.zip"]
        if sys.platform == "darwin":
            if platform.machine() == "arm64":
                return ["-bin-macos-arm64.tar.gz"]
            return ["-bin-macos-x64.tar.gz"]
        return []

    def _fetch_release(self, patterns: list[str]) -> tuple[dict, dict]:
        """查询 llama.cpp 最新发布（GitHub API 直连优先，代理兜底），返回 (release, asset)。

        资产名形如 llama-b11182-bin-win-cuda-12.4-x64.zip：必须以
        llama-{tag}-bin- 开头匹配，避免误选 cudart-llama-* 等 CUDA 运行时
        伴随包（CUDA 构建随后按需单独下载伴随包）。
        """
        headers = {
            "User-Agent": "SubtitleDubbingStudio",
            "Accept": "application/vnd.github+json",
        }
        last_err: Exception | None = None
        for api in (LLAMA_RELEASES_API, GH_PROXY + LLAMA_RELEASES_API):
            try:
                resp = requests.get(api, headers=headers, timeout=20)
                resp.raise_for_status()
                releases = resp.json()
                for rel in releases:
                    if not isinstance(rel, dict):
                        continue
                    prefix = f"llama-{rel.get('tag_name', '')}-bin-"
                    for pattern in patterns:
                        for asset in rel.get("assets", []):
                            name = str(asset.get("name", ""))
                            if name.startswith(prefix) and name.endswith(pattern):
                                return rel, asset
            except (requests.RequestException, ValueError) as e:
                last_err = e
                continue
        raise RuntimeError(
            f"无法获取 llama.cpp 最新发布信息（GitHub API 与加速代理均不可达）：{last_err}"
        )

    def _download_file(self, url: str, dest: Path) -> None:
        """流式下载到 dest，进度经 step_changed 上报；gh-proxy 优先、直连兜底。"""
        last_err: Exception | None = None
        for candidate in (GH_PROXY + url, url):
            try:
                self._download_stream(candidate, dest)
                return
            except _Cancelled:
                raise
            except requests.RequestException as e:
                last_err = e
                logger.warning("下载 llama-server 失败（%s），换下一个源重试: %s", candidate, e)
        raise RuntimeError(f"下载 llama-server 失败（加速代理与直连均失败）：{last_err}")

    def _download_stream(self, url: str, dest: Path) -> None:
        step_text = tr("env.step.llama_download")
        with requests.get(url, stream=True, timeout=(10, 60)) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            start = time.time()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if self._cancel.is_set():
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    elapsed = max(time.time() - start, 0.1)
                    if total > 0:
                        self.step_changed.emit(
                            f"{step_text} {done / total * 100:.0f}% · {done / elapsed / 1e6:.1f} MB/s"
                        )
                    else:
                        self.step_changed.emit(
                            f"{step_text} {done / 1e6:.0f} MB · {done / elapsed / 1e6:.1f} MB/s"
                        )
        if self._cancel.is_set():
            raise _Cancelled()

    # ------------------------------------------------------------------ 子进程

    def _pip_install(self, packages: list[str], step_text: str, no_deps: bool) -> None:
        """在 .venv-tts 内执行 pip install。"""
        cmd = [str(get_tts_venv_python()), "-m", "pip", "install", "--disable-pip-version-check"]
        if no_deps:
            cmd.append("--no-deps")
        cmd.extend(packages)
        self._run(cmd, step_text)

    def _run(self, cmd: list[str], step_text: str | None = None) -> None:
        """执行子进程，stdout 按行回流为详细日志；非零退出码抛 RuntimeError。"""
        if step_text:
            self.step_changed.emit(step_text)
        try:
            self._proc = subprocess.Popen(
                [str(c) for c in cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(AppConfig.APP_ROOT),
                creationflags=_CREATION_FLAGS,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"未找到可执行程序（{cmd[0]}）：{e}") from e
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.detail_line.emit(line)
        returncode = proc.wait()
        self._proc = None
        if self._cancel.is_set():
            raise _Cancelled()
        if returncode != 0:
            shown = " ".join(str(c) for c in cmd[:6])
            raise RuntimeError(f"命令执行失败（退出码 {returncode}）：{shown} …")


# ---------------------------------------------------------------- 管理器

class EnvSetupManager(QObject):
    """环境搭建单例管理器：QThread 生命周期 + 信号转发（UI 与后台解耦）。

    信号：
        setup_started(): 搭建已开始
        setup_progress(str): 当前步骤文案（含下载百分比）
        setup_finished(bool, str): 搭建结束（成功与否, 完整结果/错误文案）
        setup_cancelled(): 用户取消
        environment_changed(): 环境就绪状态可能已变化（供设备卡等刷新）
    """

    setup_started = Signal()
    setup_progress = Signal(str)
    setup_finished = Signal(bool, str)
    setup_cancelled = Signal()
    environment_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _EnvSetupThread | None = None

    def is_running(self) -> bool:
        """是否有搭建任务进行中。"""
        return self._thread is not None and self._thread.isRunning()

    def start_setup(self, device: DeviceInfo | None) -> bool:
        """启动环境搭建（补齐缺失部分）；已在进行中时返回 False。"""
        if self.is_running():
            return False
        self._thread = _EnvSetupThread(device)
        self._thread.step_changed.connect(self.setup_progress)
        self._thread.done.connect(self._on_done)
        self._thread.failed.connect(self._on_failed)
        self._thread.cancelled.connect(self._on_cancelled)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()
        self.setup_started.emit()
        return True

    def cancel(self) -> None:
        """取消进行中的搭建。"""
        if self._thread is not None:
            self._thread.cancel()

    # ------------------------------------------------------------------ 结果

    def _on_done(self, results: dict) -> None:
        ok = bool(results.get("tts_ready")) and bool(results.get("llama_ready"))
        detail = (
            f"{tr('bar.env_tts')}：{_status_text(results.get('tts_ready'))}；"
            f"{tr('bar.env_llama')}：{_status_text(results.get('llama_ready'))}"
        )
        self.setup_finished.emit(ok, tr("env.setup_done", detail=detail))
        self.environment_changed.emit()

    def _on_failed(self, message: str) -> None:
        self.setup_finished.emit(False, tr("env.setup_failed", error=message))
        self.environment_changed.emit()

    def _on_cancelled(self) -> None:
        self.setup_cancelled.emit()
        self.environment_changed.emit()

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None


def _status_text(ok: bool | None) -> str:
    return tr("common.ready") if ok else tr("common.not_ready")


# 模块级单例：通过 get_env_setup_manager() 获取
_MANAGER: EnvSetupManager | None = None


def get_env_setup_manager() -> EnvSetupManager:
    """获取 EnvSetupManager 单例（首次调用时创建）。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = EnvSetupManager()
    return _MANAGER
