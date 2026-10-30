# -*- coding: utf-8 -*-
"""运行环境搭建：ML 推理环境（.venv-ml）+ TTS 独立环境（.venv-tts）+ llama-server。

设置窗口"运行环境"行 / 资源栏推理设备卡对应的后台逻辑：
- ML 环境（人声分离 demucs + MOSS ASR）：ASR 的 remote code 需要
  transformers 5.x，与 TTS 所需 4.x 冲突，两个 venv 严禁合并；冻结态
  （Nuitka 安装包）主程序无 Python 运行时，ML 推理跑在独立的 .venv-ml
  （自装 torch + transformers 5.x + demucs）。开发态直接复用主环境
  （worker 用当前解释器运行，不搭建 .venv-ml）。
- TTS 环境：检测复用 app.pipeline.tts.tts_runtime_ready()；搭建步骤复刻
  已手工验证的流程——venv 创建并写 .pth 共享 torch/numpy（开发态共享
  主环境；冻结态共享 .venv-ml，方向单一：.venv-tts 只借不外借）、
  IndexTTS 源码 zip 快照下载到 third_party/index-tts（不依赖 git）、
  核心依赖 --no-deps 安装（防止 pip 重解析整套版本树）、transformers
  4.52 自装以遮蔽共享方的 5.x（.venv-tts 自身 site-packages 优先级
  更高）、torchaudio 2.8 与共享 torch 2.8.0 配套（装完仍剥离 C 扩展，
  只留已长期验证的纯 Python 层）、g2p_en 的 nltk 词典数据预置到
  .venv-tts/nltk_data（避免 import g2p_en 时 nltk.download 联网
  挂死）、--no-deps 包的传递依赖按部署 venv 实际元数据闭包核对后
  显式补齐（numba / llvmlite / future / librosa 等，见 _SUB_DEPS）。
- LLAMA 服务：llama.cpp 预编译版只在 GitHub Releases 发布，经 gh-proxy.com
  加速下载（直连兜底），按平台/设备选包（Windows：有 N 卡选 CUDA 构建，
  否则 CPU 构建；macOS：arm64 / x64），解压到 tools/llama.cpp/。CUDA 构建
  需一并下载 cudart 伴随包（cublas/cudart 运行库，ggml-cuda.dll 动态链接
  它们；缺失时 llama-server 静默回退 CPU）。

冻结态的 venv 创建基础解释器：安装包内嵌便携 CPython
（runtime/python，python-build-standalone），开发态用当前解释器。

所有步骤幂等：已完成部分（venv 齐备、源码在位、llama-server 在位）自动
跳过，失败或取消后重跑只补缺失部分。

pip 安装一律国内镜像优先（_PIP_INDEXES：清华 / 阿里，末位空串为官方
默认源兜底），单源失败（限速中断 / 连接重置 / 镜像缺包）自动换下一个
源整体重跑——pip 先完成全部下载才开始安装，中途失败不产生半装状态。

全过程（步骤 / 完整命令 / 子进程全部输出行 / 下载与失败堆栈）写入
用户配置目录的部署日志 deploy日期.log（与 startup 日志同目录，见
app/resources/deploy_log.py），出错时错误信息附带该日志路径。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, Signal

from app.core.config import AppConfig, is_frozen, sanitized_subprocess_env
from app.core.hardware import DeviceInfo
from app.core.i18n import tr
from app.pipeline.llm_engine import resolve_llama_server_exe
from app.pipeline.ml_client import (
    ML_VENV_DIR,
    get_ml_venv_python,
    ml_runtime_ready,
)
from app.pipeline.tts import (
    INDEX_REPO_DIR,
    TTS_SMOKE_SENTINEL,
    TTS_VENV_DIR,
    get_tts_venv_python,
    tts_runtime_ready,
)
from app.resources.deploy_log import deploy_logger, deploy_log_path
from app.resources.model import move_to_trash

logger = logging.getLogger(__name__)

# GitHub 加速代理（前缀 + 完整 URL）
GH_PROXY = "https://gh-proxy.com/"

INDEXTTS_ARCHIVE = (
    "https://github.com/index-tts/index-tts/archive/refs/heads/main.zip"
)
LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
LLAMA_TOOLS_DIR = AppConfig.TOOLS_DIR / "llama.cpp"

# pip 下载源（国内镜像优先）：官方源 pypi.org / download.pytorch.org 在
# 弱网（典型国内家宽直连）下常见 20 kB/s 级限速与连接重置——124MB 的
# torch 轮子要一小时以上且中途必断（详见 deploy 日志实测）；清华 / 阿里
# 为 PyPI 全量镜像。末位空串 = 不传 -i（pip 默认官方源兜底：镜像同步
# 滞后 / 缺包时仍能完成安装）
_PIP_INDEXES: tuple[str, ...] = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "",
)

# ---------------------------------------------------------------- ML 依赖清单
# .venv-ml（冻结态）：ASR/demucs 推理环境。transformers 必须为 5.x
# （MOSS remote code 按其编写）；torch 版本与 .venv-tts 共享方一致。
# torch 统一从 PyPI 装 win_amd64 轮子（Windows 上该轮子即 CPU 构建，
# 与 pytorch.org/whl/cpu 的 +cpu 版同源同内容；官方 CPU 源会把全部
# 依赖也指向 pytorch.org 自家服务器，弱网下慢到不可用），下载源走
# _PIP_INDEXES 镜像优先。
# 钉 2.8.0（不追最新）：与 torchaudio 2.8 完全配套；torch 2.14 实测在
# 干净云主机上 import 即 c10.dll WinError 1114（DLL 初始化失败，退避
# 重试 + 强制重装均无效；同一台机本机正常，见 deploy 日志 2026-10-18
# 云主机），2.8 的 CPU 轮部署面成熟得多。transformers 5.17 / demucs /
# silero-vad / accelerate 的 torch 下限（2.5 / 2.1 / 1.12 / 2.0）均满足。
_ML_TORCH = "torch==2.8.0"
# torch/numpy 用 constraints 锁死（pip 装 demucs 等时不得重解析/升降级）
_ML_CONSTRAINTS = ["torch==2.8.0", "numpy==2.5.3"]
# 其余依赖：正常带依赖安装（constraints 保证共享的 torch/numpy 不被动）
# PyTorch 官方 CUDA 轮子源（torch==2.8.0+cu126）：PyPI / 国内镜像源的
# Windows torch 轮子均为 CPU 构建，CUDA 设备必须走此源，否则装上后
# torch.cuda.is_available() 恒为 False、全程 CPU 推理（实测 GTX 1660 Ti
# 任务日志 "Torch not compiled with CUDA enabled"，2026-10-19）；
# ==2.8.0 可匹配 2.8.0+cu126（PEP 440 忽略 local version）
_TORCH_CU_INDEX = "https://download.pytorch.org/whl/cu126"

_ML_PACKAGES = [
    "transformers==5.17.0",
    "accelerate==1.15.0",
    "demucs==4.1.0",
    "silero-vad==6.2.3",
    "soundfile==0.14.0",
    "numpy==2.5.3",
    # MOSS ASR 的 load_audio（transformers 音频解码路径）运行时懒 import
    # librosa——transformers 元数据不声明该依赖，闭包核对发现不了，缺它
    # 转写直接失败（实测 "load_audio requires the librosa library"，任务
    # 日志 2026-10-19）；带依赖安装（soxr/numba 等一并就位）
    "librosa==1.0.0",
]

# .venv-ml 内执行的导入冒烟测试（transformers 必须为 5.x；torchaudio 为
# 剥离 C 扩展后的纯 Python 层——demucs 的 convert_audio 依赖其
# functional.resample（纯 torch 实现），一并真实调用验证）。
# 逐模块 import 打印进度（同 _SMOKE_IMPORTS，超时可定位卡点）
_ML_SMOKE_IMPORTS = (
    "import sys\n"
    "mods = ['torch', 'numpy', 'soundfile', 'transformers', 'accelerate', "
    "'silero_vad', 'demucs', 'librosa', 'torchaudio']\n"
    "for m in mods:\n"
    "    print('[import] ' + m, flush=True)\n"
    "    globals()[m] = __import__(m)\n"
    "torchaudio.functional.resample(torch.zeros(1, 480), 48000, 24000)\n"
    "sys.exit(0 if int(transformers.__version__.split('.')[0]) >= 5 else 3)"
)

# ---------------------------------------------------------------- 依赖清单
# 版本与手工验证通过的现场环境完全一致（.venv-tts 逐一核对 dist-info）

# 核心：必须 --no-deps 安装——防止 pip 重新解析整套版本树（whisper 等
# 声明无上限的 torch 依赖、audiotools 的 protobuf<3.20 旧约束，都会在
# 干净环境里引发不必要的升降级 / 冲突），共享 torch 2.8 恰好满足全部
# 核心包的 torch 要求，元数据在位即被 pip 视为满足
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
# 纯 Python 包，不触碰共享环境）。--no-deps 包（_NO_DEPS_CORE /
# _NO_DEPS_RUNTIME）声明的传递依赖必须在此显式补齐：闭包核对以部署
# venv 的实际元数据为准（.venv-tts ∪ .pth 共享的 .venv-ml 并集，即
# 运行时 import 的真实可见集），不能以开发主环境"已装"作参照——主环境
# 装有 numba/librosa 而部署不装，正是连续多轮 deploy 失败的漏因
# （inflect 'proces' 'numba' 'past' 逐个暴露）。剩余未装缺口仅
# pyloudnorm（audiotools 声明、纯懒加载功能且 app 无调用，实证无害）
_SUB_DEPS = [
    "matplotlib==3.11.2",
    "argbind==0.3.9",
    "ffmpy==1.0.0",
    "flatten-dict==0.5.0",
    "importlib-resources==7.1.0",
    "inflect==7.5.0",
    "ipython==9.17.1",
    "markdown2==2.5.5",
    "pystoi==0.4.1",
    "randomname==0.2.1",
    "proces==0.1.7",
    "tensorboard==2.21.0",
    "tiktoken==0.14.0",
    "typeguard==4.6.0",
    "nltk==3.10.3",
    "distance==0.1.3",
    "textsearch==0.0.24",
    "anyascii==0.3.3",
    "pyahocorasick==2.3.1",
    "pyphen==0.18.1",
    "contractions==0.1.73",
    "more-itertools==11.1.0",
    # openai-whisper（--no-deps 安装）传递依赖：whisper/timing.py 顶部
    # import numba；llvmlite 为 numba 的配套 JIT 后端（版本强绑定，
    # 必须显式同装）。numba 0.67 + llvmlite 0.49 + 共享 numpy 2.5.3
    # 与开发主环境同组合验证
    "numba==0.67.0",
    "llvmlite==0.49.0",
    # ffmpeg-python（--no-deps 安装）传递依赖：nodes.py 顶部
    # from past.builtins import basestring——past 模块由 future 包提供，
    # 缺失时 import ffmpeg 直接 No module named 'past'（此前误判为纯
    # 元数据声明，实测复现）
    "future==1.0.0",
    # librosa 为 .venv-tts 必装项：vendored index-tts 引擎
    # (infer_v2_5.py) 顶层 import librosa，加载即需。主程序自 2026-10-21
    # 起不依赖 librosa（mix 重采样改 soxr、参考音频规范化回退改
    # soundfile+soxr、时长拉伸仅 ffmpeg），Nuitka 冻结包不再编入
    # librosa/numba——故必须由此闭包为 worker 独立提供完整依赖链
    "librosa==1.0.0",
    "soxr==1.1.0",
    "lazy-loader==0.5",
    "msgpack==1.2.2",
    "pooch==1.9.0",
    "decorator==5.3.1",
    "scikit-learn==1.9.1",
    # threadpoolctl 为 scikit-learn 传递依赖；PyPI 最高 3.7.0
    #（requirements.txt 的 3.7.1 为笔误，装不到）
    "threadpoolctl==3.7.0",
    # omegaconf（+ 其唯一传递依赖 antlr4-python3-runtime）：vendored
    # third_party/index-tts 的顶层 import 需求（infer_v2_5.py 顶部
    # `from omegaconf import OmegaConf`）。闭包核对天然看不见 vendored
    # 包的依赖——它不经 pip 安装、元数据无从扫描（官方 pyproject 声明
    # omegaconf>=2.3.0，实测 2.3.0 可用），此类缺口只能在 import 链
    # 实测中暴露（deploy 日志 2026-10-19 两机 TTS 模型加载报
    # No module named 'omegaconf'）。modelscope 同为官方声明的顶层
    # import（infer_v2_5.py `from modelscope import AutoModelForCausalLM`），
    # 但其依赖树过重（数十个传递依赖）且仅服务于可选的 QwenEmotion
    # 情感分析（use_qwen_emo，默认关闭、worker 未启用），已改为
    # ImportError 时退化到等价的 transformers 路径，不在依赖闭包内
    "omegaconf==2.3.0",
    "antlr4-python3-runtime==4.9.3",
]

# torchaudio 2.8 的 C 扩展与共享 torch 2.8 理论上配套可加载；仍统一剥离
# 只留纯 Python 层——demucs / openunmix 运行期仅用到纯 torch 实现的
# functional.resample，C 扩展在部分机器（云主机，见 ML 步骤 3 注释）
# 加载不稳，纯 Python 面已长期验证
_TORCHAUDIO_C_EXTS = [
    "_torchaudio.pyd",
    "_torchaudio.so",
    "libtorchaudio.pyd",
    "libtorchaudio.so",
]

# .venv-tts 内执行的导入冒烟测试（覆盖全部 --no-deps 安装的包；
# numba 为 whisper 硬依赖、librosa 为 worker 回退路径依赖，一并覆盖）。
# 逐模块 import 并实时打印进度：某个依赖 import 时联网（如 nltk 数据
# 缺失触发 nltk.download）或 DLL 加载挂死时，超时后输出尾部直接显示
# 卡在哪一个模块（见 _smoke_run 的 TimeoutExpired 处理）。
# 注意只 import 不调功能：librosa 首次功能调用会经 lazy_loader 懒加载
# notation.py，其模块级 @njit(cache=True) 触发 numba 磁盘缓存初始化
# （往 __pycache__ 建探测临时文件），杀毒软件实时扫描新建文件时可出现
# 分钟级停顿（实测 faulthandler 栈定位于 tempfile 创建）——import 链路
# 完全不走这条路径
_SMOKE_IMPORTS = (
    "import sys\n"
    f"sys.path.insert(0, r'{INDEX_REPO_DIR}')\n"
    "mods = ['transformers', 'torchaudio', 'matplotlib', 'audiotools', "
    "'torch_stoi', 'json5', 'wetext', 'g2p_en', 'cn2an', 'jieba', "
    "'whisper', 'numba', 'librosa', 'sentencepiece', 'fugashi', "
    "'unidic_lite', 'textstat', 'ffmpeg', 'munch', 'argbind', 'pystoi', "
    "'randomname', 'nltk', 'tiktoken', 'omegaconf']\n"
    "for m in mods:\n"
    "    print('[import] ' + m, flush=True)\n"
    "    globals()[m] = __import__(m)\n"
    "print('[import] indextts.infer_v2_5', flush=True)\n"
    "import indextts.infer_v2_5\n"
    "sys.exit(0)"
)

# Windows 访问冲突退出码（0xC0000005 = 3221225477）：C 扩展 .pyd 文件
# 损坏（磁盘满时 pip 解压装坏的 sentencepiece，实测见 deploy 日志
# 2026-10-18 云主机——该轮安装列表无 sentencepiece，坏文件是旧轮次
# 留下、此前每轮都被更早的失败挡住从未走到）或杀软注入冲突，进程
# 无 traceback 直接死亡。自愈策略：按逐模块输出定位崩住的模块，
# --force-reinstall --no-deps 重装（--no-cache-dir 防缓存 wheel 同样
# 损坏）后复验一次
_ACCESS_VIOLATION = 3221225477

# 冒烟模块名 → PyPI 项目名（仅列 import 名与项目名不同的；同名走默认）。
# 版本不在此维护：从 venv 现场已装的 dist-info 目录名读取，钉住当前
# 版本重装，防止重装引入版本漂移
_IMPORT_PKG_OVERRIDES = {
    "audiotools": "descript-audiotools",
    "torch_stoi": "torch-stoi",
    "g2p_en": "g2p-en",
    "whisper": "openai-whisper",
    "silero_vad": "silero-vad",
    "unidic_lite": "unidic-lite",
    "ffmpeg": "ffmpeg-python",
}

# 访问冲突自愈的版本回退表：崩点包对当前版本的 .pyd 在本机初始化即
# 崩溃（重装同版本无效，实测云主机 sentencepiece，deploy 日志
# 2026-10-19）时逐个降版本尝试。sentencepiece 0.1.99–0.2.2 对
# transformers 的 SentencePieceProcessor 接口完全兼容，IndexTTS 生态
# 对其版本无要求
_PKG_FALLBACK_VERSIONS: dict[str, tuple[str, ...]] = {
    # 仅收有 cp313 Windows wheel 的版本（PyPI 实查 cp313-cp313-win_amd64
    # 仅 0.2.1/0.2.2）：0.2.0 / 0.1.99 无 cp313 轮子，pip 会退回 sdist
    # 现场编译，机器上无 cmake 即 WinError 2（云主机实测 2026-10-19），
    # 既修不好又把真实病灶（加载即 0xC0000005）淹没在无关报错里
    "sentencepiece": ("0.2.1",),
}

# g2p_en 所需的 nltk 数据（zip 原样放置，见 _download_nltk_data）：
# (相对 nltk_data gh-pages packages 的路径, nltk_data 内子目录)
_NLTK_BASE = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/"
)
_NLTK_RESOURCES: tuple[tuple[str, str], ...] = (
    ("corpora/cmudict.zip", "corpora"),
    ("taggers/averaged_perceptron_tagger.zip", "taggers"),
    ("taggers/averaged_perceptron_tagger_eng.zip", "taggers"),
)

# DLL 加载类失败的识别模式：多见于杀毒软件实时扫描/拦截新释放的 DLL
# （云主机/慢盘上扫描大体积 torch DLL 可持续数分钟，期间 DllMain 被阻塞，
# 安装完立刻导入必失败、稍后重试即通过——实测见 deploy 日志 2026-10-17
# 云主机 WinError 1114，同机晚几分钟重跑即过），或安装介质上的二进制
# 损坏；命中时按"等待→强制重装 torch（--force-reinstall --no-cache-dir，
# 避免复用损坏的缓存 wheel）→再等待"退避重试（见 _smoke_with_dll_retry），
# 仍失败则在错误信息中给出处理指引
_DLL_FAILURE_RE = re.compile(
    r"WinError (1114|126|127|193)|动态链接库|DLL 初始化|DLL initialization"
)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class _Cancelled(Exception):
    """用户主动取消。"""


# ---------------------------------------------------------------- 状态查询

def environment_status() -> dict:
    """当前运行环境状态：ML + TTS 虚拟环境 + llama-server（均只做文件级检查，毫秒级）。"""
    ml_ok, ml_detail = ml_runtime_ready()
    tts_ok, tts_detail = tts_runtime_ready()
    return {
        "ml_ready": ml_ok,
        "ml_detail": ml_detail,
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
    """torch/numpy 共享方 site-packages（.venv-tts 经 .pth 借用）。

    - 开发态：主环境（当前解释器）site-packages；
    - 冻结态：.venv-ml 的 site-packages（主程序无 Python 运行时，
      torch 只存在于 .venv-ml；.venv-tts 自装 transformers 4.52 遮蔽
      共享方 5.x，机制与开发态遮蔽主环境 5.x 一致）。
    """
    if is_frozen():
        if os.name == "nt":
            return ML_VENV_DIR / "Lib" / "site-packages"
        lib = ML_VENV_DIR / "lib"
        for p in lib.glob("python*/site-packages"):
            if p.is_dir():
                return p
        return lib / "site-packages"
    return Path(sysconfig.get_paths()["purelib"])


def _ml_venv_site_packages() -> Path:
    """定位 .venv-ml 的 site-packages 目录（跨平台）。"""
    if os.name == "nt":
        return ML_VENV_DIR / "Lib" / "site-packages"
    lib = ML_VENV_DIR / "lib"
    for p in lib.glob("python*/site-packages"):
        if p.is_dir():
            return p
    return lib / "site-packages"


def _bootstrap_python() -> Path:
    """创建两个 venv 的基础解释器。

    开发态：当前解释器（主环境，--system-site-packages 才有意义）；
    冻结态：安装包内嵌的便携 CPython（runtime/python）——冻结可执行中
    ``sys.executable -m venv`` 无法工作（无标准库 venv 模块与 ensurepip）。
    """
    if not is_frozen():
        return Path(sys.executable)
    py = AppConfig.runtime_python_exe()
    if not py.is_file():
        raise RuntimeError(
            f"安装包缺少内置 Python 运行时（{py}），无法创建虚拟环境"
        )
    return py


def _venv_site_packages() -> Path:
    """定位 .venv-tts 的 site-packages 目录（跨平台）。"""
    if os.name == "nt":
        return TTS_VENV_DIR / "Lib" / "site-packages"
    lib = TTS_VENV_DIR / "lib"
    for p in lib.glob("python*/site-packages"):
        if p.is_dir():
            return p
    return lib / "site-packages"


def _strip_torchaudio_c_exts(site_packages: Path) -> list[str]:
    """剥离 torchaudio 的 C 扩展，返回被删除的文件名列表（幂等）。

    C 扩展与共享 torch 2.8 理论配套；剥离只留纯 Python 层
    （functional.resample 等，纯 torch 实现），规避部分机器上 C 扩展
    DLL 加载不稳（WinError 1114 / 127）。.venv-ml（demucs）与
    .venv-tts 共用本函数。
    """
    removed: list[str] = []
    ta_lib = site_packages / "torchaudio" / "lib"
    for name in _TORCHAUDIO_C_EXTS:
        ext = ta_lib / name
        if ext.is_file():
            ext.unlink()
            removed.append(name)
    return removed


# ---------------------------------------------------------------- 环境清除

def runtime_env_components() -> list[tuple[str, Path]]:
    """已部署运行环境的组件清单 [(组件名, 路径)，…]，仅列实际存在的目录。

    运行环境 = .venv-ml（冻结态 ML 推理）+ .venv-tts + tools（llama-server
    等工具）+ third_party/index-tts 源码克隆。开发态主解释器（项目根
    venv/）不属于部署产物，不在清单内——清除部署不影响开发环境。
    """
    candidates: list[tuple[str, Path]] = [
        (".venv-ml", ML_VENV_DIR),
        (".venv-tts", TTS_VENV_DIR),
        ("tools", AppConfig.TOOLS_DIR),
        ("third_party/index-tts", INDEX_REPO_DIR),
    ]
    return [(label, path) for label, path in candidates if path.exists()]


def delete_runtime_env() -> list[str]:
    """清除已部署的运行环境：各组件移入同级 .trash 回收目录（可找回）。

    返回被清除的组件名列表（空列表 = 本机无可清除内容）。部分组件被
    占用时（如 TTS 任务进行中，worker 正使用 .venv-tts 的 python.exe）
    Windows 下移动失败——此时抛 RuntimeError 指明失败组件与原因；已
    成功移入回收目录的其余组件保持已清除状态（重新一键部署会补齐）。
    """
    dlog = deploy_logger()
    dlog.info("=== 清除运行环境 ===（组件：%s）",
              "、".join(label for label, _ in runtime_env_components()) or "（无）")
    moved: list[str] = []
    errors: list[str] = []
    for label, path in runtime_env_components():
        try:
            dest = move_to_trash(path)
            moved.append(label)
            dlog.info("[清除] %s → %s", label, dest)
        except OSError as e:
            errors.append(f"{label}：{e}")
            dlog.error("[清除] %s 失败：%s", label, e)
    if errors:
        raise RuntimeError(
            "部分组件清除失败（多因被正在运行的任务占用，请先停止任务后重试）：\n"
            + "\n".join(errors)
        )
    dlog.info("=== 运行环境已清除：%s ===", "、".join(moved) if moved else "（无可清除组件）")
    return moved


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
        # 部署日志（配置目录 deploy日期.log）：步骤 / 完整命令 / 子进程
        # 全部输出 / 失败堆栈，见 app/resources/deploy_log.py
        self._dlog = deploy_logger()

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
        dlog = self._dlog
        dlog.info(
            "=== 环境搭建开始 ===（frozen=%s，device=%s）", is_frozen(), self._device
        )
        dlog.info(
            "APP_ROOT=%s，DATA_ROOT=%s，venv 基础解释器=%s",
            AppConfig.APP_ROOT, AppConfig.DATA_ROOT, _bootstrap_python(),
        )
        # CPU 型号一并记录：DLL 加载类失败排查时区分"杀软拦截"与
        # "CPU 过旧缺少新指令集"（云主机常见老至强）需要此信息
        dlog.info("CPU=%s", os.environ.get("PROCESSOR_IDENTIFIER", "未知"))
        try:
            results: dict = {}

            # ---- ML 推理环境（冻结态搭建 .venv-ml；开发态主环境即就绪） ----
            self.step_changed.emit(tr("env.step.check_ml"))
            ml_ok, _ = ml_runtime_ready()
            if not ml_ok and is_frozen():
                self._setup_ml()
                ml_ok, detail = ml_runtime_ready()
                if not ml_ok:
                    raise RuntimeError(f"ML 环境搭建后仍未通过检查：{detail}")
            results["ml_ready"] = ml_ok

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
            dlog.info("=== 环境搭建完成 ===：%s", results)
        except _Cancelled:
            dlog.info("=== 环境搭建被用户取消 ===")
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - 线程边界统一上报
            logger.exception("环境搭建失败")
            dlog.exception("=== 环境搭建失败 ===")
            self.failed.emit(self._failed_message(e))

    def _failed_message(self, exc: Exception) -> str:
        """失败上报文案：异常信息 + 部署日志路径（弹窗中可直接定位日志）。"""
        msg = f"{type(exc).__name__}: {exc}"
        path = deploy_log_path()
        if path is not None:
            msg += f"\n{tr('common.log_hint', path=path)}"
        return msg

    # ------------------------------------------------------------------ ML

    def _setup_ml(self) -> None:
        """搭建 .venv-ml（冻结态；全部步骤幂等，已存在的自动跳过）。

        transformers 5.x 与 TTS 所需 4.x 冲突，故本环境完全独立
        （不共享任何 site-packages）；torch/numpy 与 .venv-tts 共享方
        版本一致——.venv-tts 的 .pth 指到这里借 torch/numpy。
        """
        dlog = self._dlog
        # 0. 清理可能锁定 venv 文件的残留 worker 进程（幂等）
        self._kill_stale_venv_pythons()
        # 1. 创建虚拟环境（便携 CPython 基础上，完全独立；ensurepip
        #    失败自诊断 + 清空重试一次，见 _create_venv）
        self._create_venv(
            ML_VENV_DIR, get_ml_venv_python(), tr("env.step.ml_venv"), tag="ML",
        )

        site_packages = _ml_venv_site_packages()

        # 2. 安装 torch。CUDA 设备必须用 PyTorch 官方 CUDA 轮子源
        #    （_TORCH_CU_INDEX）：PyPI / 镜像源的 Windows torch 轮子是
        #    CPU 构建，装上后 torch.cuda.is_available() 恒为 False，
        #    demucs/ASR 全程 CPU 推理。官方源失败自动回退 CPU 版保证
        #    环境可用（日志明示 GPU 不可用，任务仍能跑）
        has_cuda = self._device is not None and self._device.device == "cuda"
        # 已装 CPU 版而本机有 CUDA：判残缺强制重装 CUDA 版
        # （torch/lib/torch_cuda.dll 仅 CUDA 构建携带）
        need_cuda_rebuild = (
            has_cuda
            and (site_packages / "torch").is_dir()
            and not (site_packages / "torch" / "lib" / "torch_cuda.dll").is_file()
        )
        if need_cuda_rebuild:
            dlog.warning("[ML] 已装 torch 为 CPU 构建，但检测到 CUDA 设备，重装 CUDA 版")
        if (site_packages / "torch").is_dir() and not need_cuda_rebuild:
            dlog.info("[ML] torch 已安装，跳过")
        else:
            step = tr("env.step.ml_torch_cuda" if has_cuda else "env.step.ml_torch_cpu")
            if has_cuda:
                try:
                    self._pip_install(
                        [_ML_TORCH], step, no_deps=False,
                        python=get_ml_venv_python(), indexes=[_TORCH_CU_INDEX],
                        # --no-cache-dir：2.9GB 的 CUDA 轮子再进 pip 缓存
                        # 目录（系统盘）必爆 Errno 28，一次性中转安装即可
                        extra_args=(
                            ["--force-reinstall", "--no-cache-dir"]
                            if need_cuda_rebuild
                            else ["--no-cache-dir"]
                        ),
                    )
                except RuntimeError as e:
                    dlog.warning("[ML] PyTorch 官方 CUDA 源安装失败，回退 CPU 版：%s", e)
                    self._pip_install(
                        [_ML_TORCH], step, no_deps=False,
                        python=get_ml_venv_python(),
                    )
            else:
                self._pip_install(
                    [_ML_TORCH], step, no_deps=False,
                    python=get_ml_venv_python(),
                )

        # 3. torchaudio 兼容层：demucs 的依赖树（demucs / openunmix）要求
        #    torchaudio。与 .venv-tts 同法：--no-deps 安装 torchaudio 2.8
        #    后剥离 C 扩展只留纯 Python 层——元数据在位即满足 demucs 的
        #    依赖要求，pip 不再对其解析（运行期 demucs 只用到纯 torch 实现
        #    的 functional.resample，纯 Python 面已长期验证；C 扩展在部分
        #    云主机上加载不稳，见 _ML_TORCH 注释）
        if (site_packages / "torchaudio").is_dir():
            dlog.info("[ML] torchaudio 已安装，跳过安装（仍执行剥离）")
        else:
            self._pip_install(
                ["torchaudio==2.8.0"], tr("env.step.ml_torchaudio"),
                no_deps=True, python=get_ml_venv_python(),
            )
        removed = _strip_torchaudio_c_exts(site_packages)
        if removed:
            dlog.info("[ML] 已剥离 torchaudio C 扩展：%s", ", ".join(removed))

        # 4. 写 constraints（锁 torch/numpy，后续安装不得重解析共享件）
        constraints = ML_VENV_DIR / "constraints.txt"
        constraints.write_text(
            "\n".join(_ML_CONSTRAINTS) + "\n", encoding="utf-8"
        )
        dlog.info("[ML] constraints 已写入：%s", constraints)

        # 5. 安装其余依赖（transformers 5.x / demucs / silero-vad 等）
        self._pip_install(
            _ML_PACKAGES, tr("env.step.ml_pip"),
            no_deps=False, python=get_ml_venv_python(),
            constraints=constraints,
        )

        # 6. 导入冒烟测试（transformers 必须 5.x）；DLL 加载类失败按
        #    "等待→重装 torch→再等待"退避重试（见 _smoke_with_dll_retry）
        self.step_changed.emit(tr("env.step.ml_verify"))

        def _repair() -> None:
            # CUDA 设备重装 torch 必须仍走 CUDA 源（否则换回 CPU 版）
            self._pip_install(
                [_ML_TORCH], tr("env.step.ml_torch_repair"),
                no_deps=False, python=get_ml_venv_python(),
                constraints=constraints,
                extra_args=["--force-reinstall", "--no-cache-dir"],
                indexes=[_TORCH_CU_INDEX] if has_cuda else None,
            )

        proc = self._smoke_with_dll_retry(
            get_ml_venv_python(), _ML_SMOKE_IMPORTS, repair=_repair
        )
        if proc.returncode == _ACCESS_VIOLATION:
            # 同 TTS 侧：C 扩展 .pyd 损坏时无 traceback 直接死亡，
            # 强制重装崩点模块后复验
            proc = self._repair_crashed_import(
                proc, _ML_SMOKE_IMPORTS, get_ml_venv_python(),
                _ml_venv_site_packages(),
            )
        if proc.returncode != 0:
            tail = self._output_tail(proc)
            if proc.returncode == _ACCESS_VIOLATION:
                raise RuntimeError(
                    f"ML 依赖导入验证失败（访问冲突 0xC0000005，已自动"
                    f"重装与版本回退均未通过）：\n{tail}\n\n"
                    "该模块 C 扩展（.pyd）在本机加载即崩溃且换版本无效，"
                    "常见原因：安全软件 / 系统防护拦截进程加载新 DLL。"
                    "请把安装目录加入安全软件信任/排除列表后重试部署；"
                    "仍失败请把部署日志发回分析。"
                )
            if self._looks_like_dll_failure(tail):
                raise RuntimeError(
                    f"ML 依赖导入验证失败（已自动等待重试并重装 torch 仍不通过）：\n"
                    f"{tail}\n\nCPU：{os.environ.get('PROCESSOR_IDENTIFIER', '未知')}\n"
                    f"{tr('env.dll_failure_hint')}"
                )
            raise RuntimeError(f"ML 依赖导入验证失败：\n{tail}")
        dlog.info("[ML] 冒烟测试通过")

    # ------------------------------------------------------------------ TTS

    def _setup_tts(self) -> None:
        """搭建 .venv-tts（全部步骤幂等，已存在的自动跳过）。"""
        dlog = self._dlog
        # 0. 冒烟通过哨兵先删（幂等），冒烟实际通过后在方法末尾写回：
        #    tts_runtime_ready() 以哨兵判定"上次冒烟确实通过"，缺失即
        #    判未就绪触发重新部署——防冒烟失败后重跑被 run() 就绪门禁
        #    跳过整个 _setup_tts、坏环境被误报就绪（云主机 2026-10-19
        #    第二次部署 50 秒假成功，sentencepiece 导入实际仍崩）
        TTS_SMOKE_SENTINEL.unlink(missing_ok=True)
        # 1. 创建虚拟环境：开发态以 --system-site-packages 建在主环境上
        #    （标准库扩展可见）；冻结态基于便携 CPython（无包可共享，纯靠
        #    第 2 步的 .pth 借 .venv-ml 的 torch/numpy）——ensurepip 失败
        #    自诊断 + 清空重试一次，见 _create_venv
        self._create_venv(
            TTS_VENV_DIR,
            get_tts_venv_python(),
            tr("env.step.venv"),
            tag="TTS",
            system_site_packages=not is_frozen(),
        )

        # 2. 写 .pth 共享 torch/numpy（开发态=主环境；冻结态=.venv-ml）
        self.step_changed.emit(tr("env.step.pth"))
        site_packages = _venv_site_packages()
        site_packages.mkdir(parents=True, exist_ok=True)
        pth = site_packages / "_share_main_venv.pth"
        if not pth.is_file():
            pth.write_text(str(_main_site_packages()), encoding="utf-8")
            dlog.info("[TTS] 已写入 .pth 共享目录：%s", pth)

        # 3. 下载 IndexTTS 源码（GitHub main 分支 zip 快照，gh-proxy 优先
        #    直连兜底）——不用 git clone：全新部署的机器普遍未装 git
        #    （实测云主机报 WinError 2 找不到 git）
        if (INDEX_REPO_DIR / "indextts").is_dir():
            dlog.info("[TTS] IndexTTS 源码已存在，跳过下载：%s", INDEX_REPO_DIR)
        else:
            self.step_changed.emit(tr("env.step.clone"))
            self._download_indextts_source()

        # 3.4 清理可能锁定 venv 文件的残留 worker 进程（幂等）
        self._kill_stale_venv_pythons()

        # 3.5 源码补丁（幂等，须在源码就位后、冒烟前执行）：上游
        #     infer_v2_5.py 顶层 `from modelscope import AutoModelForCausalLM`
        #     在未装 modelscope 的部署环境 import 即失败。部署机源码来自
        #     GitHub zip 快照（见 _download_indextts_source），项目内已打
        #     补丁的副本不会到用户机，必须在部署现场补（deploy 日志
        #     2026-10-19 两机冒烟因此失败）
        self._patch_indextts_modelscope()
        self._patch_model_download()

        # 4-6. 依赖安装
        self._pip_install(_NO_DEPS_CORE, tr("env.step.pip_core"), no_deps=True)
        self._pip_install(_NO_DEPS_RUNTIME, tr("env.step.pip_runtime"), no_deps=True)
        self._pip_install(_SUB_DEPS, tr("env.step.pip_sub"), no_deps=False)

        # 7. 剥离 torchaudio C 扩展
        self.step_changed.emit(tr("env.step.strip"))
        removed = _strip_torchaudio_c_exts(_venv_site_packages())
        if removed:
            dlog.info("[TTS] 已剥离 torchaudio C 扩展：%s", ", ".join(removed))

        # 7.5 预置 g2p_en 所需 nltk 数据：import g2p_en 时 nltk 数据缺失
        #     会触发模块级 nltk.download 联网（网络黑洞下挂死超时）
        self._download_nltk_data()

        # 8. 导入冒烟测试（在 .venv-tts 解释器内执行）；DLL 加载类失败
        #    同样退避等待重试（torch 经 .pth 共享自 .venv-ml/主环境，
        #    不在此重装，repair 留空）
        self.step_changed.emit(tr("env.step.verify"))
        proc = self._smoke_with_dll_retry(get_tts_venv_python(), _SMOKE_IMPORTS)
        if proc.returncode == _ACCESS_VIOLATION:
            # C 扩展 .pyd 损坏（旧轮次磁盘满装坏）典型：进程无 traceback
            # 直接死亡，崩点看逐模块输出最后一个 [import]（实测云主机
            # sentencepiece，deploy 日志 2026-10-18）
            proc = self._repair_crashed_import(
                proc, _SMOKE_IMPORTS, get_tts_venv_python(),
                _venv_site_packages(),
            )
        if proc.returncode != 0:
            tail = self._output_tail(proc)
            if proc.returncode == _ACCESS_VIOLATION:
                raise RuntimeError(
                    f"TTS 依赖导入验证失败（访问冲突 0xC0000005，已自动"
                    f"重装与版本回退均未通过）：\n{tail}\n\n"
                    "该模块 C 扩展（.pyd）在本机加载即崩溃且换版本无效，"
                    "常见原因与处理（依次排查）：\n"
                    "  1. 安全软件拦截进程加载新 DLL——把安装目录加入"
                    "安全软件的信任/排除列表后重试部署；\n"
                    "  2. Windows 内置防护（与是否安装第三方杀软无关）——"
                    "PowerShell（管理员）执行：\n"
                    "     Add-MpPreference -ExclusionPath '<安装目录>'\n"
                    "  3. 系统强制的镜像随机化策略（Mandatory ASLR，部分"
                    "安全加固环境默认开启，会使个别 .pyd 初始化即崩溃而"
                    "其余扩展正常）——PowerShell（管理员）执行 "
                    "Get-ProcessMitigation -System 查看，若 ASLR 段 "
                    "ForceRelocateImages 显示为 ON，执行 "
                    "Set-ProcessMitigation -System -Disable "
                    "ForceRelocateImages 后重启再重新部署。\n"
                    "仍失败请把以上命令输出与部署日志发回分析。"
                )
            if self._looks_like_dll_failure(tail):
                raise RuntimeError(
                    f"TTS 依赖导入验证失败（已自动等待重试仍不通过）：\n"
                    f"{tail}\n\n{tr('env.dll_failure_hint')}"
                )
            raise RuntimeError(f"TTS 依赖导入验证失败：\n{tail}")

        # 冒烟实际通过：写回哨兵（run() 门禁与任务启动校验据此判就绪）
        TTS_SMOKE_SENTINEL.write_text(
            time.strftime("smoke passed %Y-%m-%d %H:%M:%S") + "\n",
            encoding="utf-8",
        )
        dlog.info("[TTS] 依赖导入冒烟通过，写入就绪哨兵")

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
        self._dlog.info(
            "[LLAMA] 选定发布 %s / 资产 %s（%.1f MB）",
            release.get("tag_name"), asset.get("name"),
            asset.get("size", 0) / 1024 / 1024,
        )

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
        dlog = self._dlog
        dlog.info(
            "[LLAMA] 下载资产 %s（%.1f MB）→ %s",
            asset.get("name"), asset.get("size", 0) / 1024 / 1024, tools_dir,
        )
        tmp = tools_dir / asset["name"]
        try:
            self._download_file(asset["browser_download_url"], tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        self.step_changed.emit(tr("env.step.llama_extract"))
        dlog.info("[LLAMA] 解压 %s → %s", tmp.name, LLAMA_TOOLS_DIR)
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
            "User-Agent": "ESubDub",
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

    def _download_file(
        self, url: str, dest: Path,
        step_text: str | None = None, log_tag: str = "LLAMA",
    ) -> None:
        """流式下载到 dest，进度经 step_changed 上报；gh-proxy 优先、直连兜底。

        step_text 缺省为 llama-server 的下载文案；下载其他资源（如
        IndexTTS 源码包）时传入对应文案，log_tag 用于部署日志前缀。
        """
        shown = step_text or tr("env.step.llama_download")
        last_err: Exception | None = None
        for candidate in (GH_PROXY + url, url):
            try:
                self._dlog.info("[%s] 尝试下载源：%s", log_tag, candidate)
                self._download_stream(candidate, dest, shown, log_tag)
                return
            except _Cancelled:
                raise
            except requests.RequestException as e:
                last_err = e
                self._dlog.warning("[%s] 下载源失败，换下一个源重试：%s", log_tag, e)
                logger.warning("下载 %s 失败（%s），换下一个源重试: %s", log_tag, candidate, e)
        raise RuntimeError(f"下载失败（加速代理与直连均失败）：{last_err}")

    def _download_stream(
        self, url: str, dest: Path,
        step_text: str | None = None, log_tag: str = "LLAMA",
    ) -> None:
        shown = step_text or tr("env.step.llama_download")
        last_log = time.monotonic()
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
                            f"{shown} {done / total * 100:.0f}% · {done / elapsed / 1e6:.1f} MB/s"
                        )
                    else:
                        self.step_changed.emit(
                            f"{shown} {done / 1e6:.0f} MB · {done / elapsed / 1e6:.1f} MB/s"
                        )
                    # 部署日志进度里程碑：每 60 秒一行（进度条信号不落盘）
                    now = time.monotonic()
                    if now - last_log >= 60:
                        total_text = (
                            f" / {total / 1e6:.0f} MB（{done / total * 100:.0f}%）"
                            if total > 0 else ""
                        )
                        self._dlog.info(
                            "[%s] %s 已下载 %.0f MB%s",
                            log_tag, dest.name, done / 1e6, total_text,
                        )
                        last_log = now
        self._dlog.info(
            "[%s] %s 下载完成（%.1f MB）", log_tag, dest.name, dest.stat().st_size / 1e6
        )
        if self._cancel.is_set():
            raise _Cancelled()

    def _download_indextts_source(self) -> None:
        """下载 IndexTTS 源码 zip 快照并解压摊平到 third_party/index-tts。

        以 GitHub main 分支的 zip 压缩包替代 git clone（与 --depth 1 等
        效），消除对 git 的依赖——全新部署的机器普遍未安装 git。zip 内
        唯一顶层目录形如 index-tts-main/，解压后移到目标位置；失败 /
        取消时清理残留，保证幂等重跑。IndexTTS 源码量级仅十来 MB，
        zip 快照不含 .git 历史，下载明显快于 clone。
        """
        dlog = self._dlog
        tmp_zip = INDEX_REPO_DIR.parent / "index-tts-main.zip"
        extract_root = INDEX_REPO_DIR.parent / ".indextts_extract"
        INDEX_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._download_file(
                INDEXTTS_ARCHIVE, tmp_zip,
                step_text=tr("env.step.clone"), log_tag="IndexTTS",
            )
            dlog.info("[IndexTTS] 解压源码包：%s", tmp_zip)
            if extract_root.exists():
                shutil.rmtree(extract_root)
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(extract_root)
            inner = [p for p in extract_root.iterdir() if p.is_dir()]
            if len(inner) != 1:
                raise RuntimeError(
                    f"IndexTTS 源码包结构异常（顶层目录数 {len(inner)}，应为 1）"
                )
            if INDEX_REPO_DIR.exists():
                shutil.rmtree(INDEX_REPO_DIR)  # 清理上次失败/取消的残留
            shutil.move(str(inner[0]), str(INDEX_REPO_DIR))
            dlog.info("[IndexTTS] 源码已就位：%s", INDEX_REPO_DIR)
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)
            tmp_zip.unlink(missing_ok=True)

    def _patch_indextts_modelscope(self) -> None:
        """给上游 infer_v2_5.py 打 modelscope 可选化补丁（幂等）。

        上游顶层 `from modelscope import AutoModelForCausalLM` 只服务于
        可选的 QwenEmotion 情感分析（use_qwen_emo，默认关闭、worker 未
        启用）；modelscope 依赖树过重（数十个传递依赖）不在部署闭包内，
        该顶层 import 使 TTS 模型加载与冒烟在部署机直接
        ModuleNotFoundError（deploy 日志 2026-10-19）。对本地 model_dir
        的 from_pretrained 而言 transformers 的 AutoModelForCausalLM
        完全等价，补丁改为 try modelscope / ImportError 时退化
        transformers——与项目内 third_party/index-tts 已手工打过的
        补丁一致（以注释标记判定幂等，两边互认）。
        """
        marker = "ESubDub 部署适配"
        target = INDEX_REPO_DIR / "indextts" / "infer_v2_5.py"
        if not target.is_file():
            return  # 源码未就位（异常路径）：冒烟环节自会报出
        text = target.read_text(encoding="utf-8")
        if marker in text:
            return  # 已打过补丁（含项目内带补丁的副本）
        old = "\nfrom modelscope import AutoModelForCausalLM\n"
        if old not in text:
            self._dlog.warning(
                "[TTS] infer_v2_5.py 未见 modelscope 顶层导入，跳过补丁"
                "（上游结构可能已调整，冒烟环节会实测 import 链）"
            )
            return
        text = text.replace(
            old,
            "\n# ESubDub 部署适配：modelscope 为可选依赖（仅 QwenEmotion 情感"
            "\n# 分析使用，默认关闭），未安装时退化到等价的 transformers 路径"
            "\ntry:"
            "\n    from modelscope import AutoModelForCausalLM"
            "\nexcept ImportError:"
            "\n    from transformers import AutoModelForCausalLM\n",
            1,
        )
        target.write_text(text, encoding="utf-8")
        self._dlog.info("[TTS] 已为 infer_v2_5.py 打 modelscope 可选化补丁")

    def _patch_model_download(self) -> None:
        """给上游 model_download.py 打 modelscope 可选化补丁（幂等）。

        上游 _snapshot_from_modelscope 在函数体内 `from modelscope.hub.
        snapshot_download import ...`——首次加载时若 {模型目录}/hf_cache/
        缺辅助模型（w2v-bert-2.0 / campplus / bigvgan），加载链的
        ensure_models_available 会现场联网下载，国内网络（need_proxy()
        为 True）首选 modelscope，缺包即 ModuleNotFoundError（实测
        2026-10-19 独显机加载 TTS 权重失败）。辅助模型已纳入应用内
        下载（model.py 的 tts_w2v / tts_campplus / tts_bigvgan），
        正常路径不再触发下载；此补丁为兜底：modelscope 缺失时退化
        huggingface_hub（HF_ENDPOINT=hf-mirror，venv 内已有）继续
        下载，不再直接崩溃。
        """
        marker = "ESubDub 部署适配"
        target = (
            INDEX_REPO_DIR / "indextts" / "utils" / "model_download.py"
        )
        if not target.is_file():
            return
        text = target.read_text(encoding="utf-8")
        if marker in text:
            return
        old = (
            "\n    from modelscope.hub.snapshot_download import "
            "snapshot_download as _ms_snapshot\n"
        )
        if old not in text:
            self._dlog.warning(
                "[TTS] model_download.py 未见 modelscope 导入，跳过补丁"
                "（上游结构可能已调整，冒烟环节会实测 import 链）"
            )
            return
        new = (
            "\n    # ESubDub 部署适配：modelscope 为可选依赖，缺失时退化"
            "\n    # huggingface_hub（HF_ENDPOINT=hf-mirror）继续下载"
            "\n    try:"
            "\n        from modelscope.hub.snapshot_download import snapshot_download as _ms_snapshot"
            "\n    except ImportError:"
            "\n        import os as _os"
            "\n        _os.environ.setdefault(\"HF_ENDPOINT\", \"https://hf-mirror.com\")"
            "\n        from huggingface_hub import snapshot_download as _hf_snapshot"
            "\n        logger.info(f\"modelscope unavailable, downloading via hf-mirror: {model_id}\")"
            "\n        _hf_snapshot(repo_id=model_id, local_dir=str(local_dir), revision=revision)"
            "\n        return local_dir\n"
        )
        text = text.replace(old, new, 1)
        target.write_text(text, encoding="utf-8")
        self._dlog.info("[TTS] 已为 model_download.py 打 modelscope 可选化补丁")

    def _download_nltk_data(self) -> None:
        """预置 g2p_en 所需 nltk 数据到 .venv-tts/nltk_data（zip 原样放置）。

        g2p_en 的 g2p.py 在模块级 nltk.download（cmudict /
        averaged_perceptron_tagger）：数据缺失时 import g2p_en 即联网，
        nltk 下载器（urllib，无超时）在 raw.githubusercontent.com 不可达
        的网络（国内云主机常见黑洞型：连不上也不快速拒绝）下挂死——
        冒烟 import 卡满 600 秒超时（实测见 deploy 日志 2026-10-18
        云主机）。nltk 3.10 的 pos_tag(lang="eng") 实际加载
        averaged_perceptron_tagger_eng，与 g2p_en 检查的旧名是两个
        资源，一并备齐（共约 5MB，经 gh-proxy 优先直连兜底下载，
        同 IndexTTS 源码）。
        落地 .venv-tts/nltk_data：nltk.data.path 自带 sys.prefix/
        nltk_data，无需环境变量；zip 先下载为 .incomplete 临时名、
        完成后原子改名；幂等（目标 zip 存在即跳过）。
        """
        data_dir = TTS_VENV_DIR / "nltk_data"
        for rel, subdir in _NLTK_RESOURCES:
            dest = data_dir / subdir / Path(rel).name
            if dest.is_file():
                self._dlog.info("[NLTK] 数据已存在，跳过：%s", dest)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".incomplete")
            self._download_file(
                _NLTK_BASE + rel, tmp,
                step_text=tr("env.step.nltk"), log_tag="NLTK",
            )
            os.replace(tmp, dest)
            self._dlog.info("[NLTK] 数据已就位：%s", dest)

    # ------------------------------------------------------------------ 子进程

    def _smoke_run(self, python: Path, script: str) -> subprocess.CompletedProcess:
        """在指定 venv 解释器内运行导入冒烟测试。

        子进程环境经 sanitized_subprocess_env() 净化：冻结态主程序目录
        （Nuitka 分发根，含随包 DLL）不进入子进程 PATH，避免 venv 内
        torch 的原生扩展按 PATH 解析依赖时命中随包 DLL 引发二进制
        不兼容（WinError 1114 等）。

        加 -I（隔离模式）：`python -c` 默认把 CWD 排进 sys.path 首位，
        冻结态 CWD=应用根目录时，随包数据文件（曾打包的 torchaudio 垫片
        等）会遮蔽 venv 真包（垫片 resample 依赖 venv 未装的 scipy →
        误报 ImportError，实测见 deploy 日志 2026-10-17）。-I 同时忽略
        PYTHON* 环境变量；venv 自身 site-packages 经 pyvenv.cfg 机制
        加载，不受影响。

        超时（600 秒）捕获为可读错误而非裸 TimeoutExpired 冒泡（实测
        见 deploy 日志 2026-10-18 云主机：import g2p_en 触发 nltk 数据
        下载，网络黑洞下 nltk 下载器无超时挂死）：冒烟脚本逐模块打印
        [import] 进度，超时时已捕获输出（TimeoutExpired.stdout）的
        尾部即卡点。
        """
        dlog = self._dlog
        dlog.info("[冒烟] %s -I -c %s", python, script)
        try:
            proc = subprocess.run(
                [str(python), "-I", "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                cwd=str(AppConfig.DATA_ROOT),
                env=sanitized_subprocess_env(),
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.TimeoutExpired as e:
            partial = e.stdout or e.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            tail = str(partial).strip()[-600:]
            dlog.error("[冒烟] 超时（600 秒），已捕获输出末尾：\n%s", tail)
            raise RuntimeError(
                "依赖导入验证超时（600 秒）：\n"
                f"{tail}\n\n"
                "导入链卡死。末尾最后一个 [import] 即卡住的模块。常见原因："
                "该依赖 import 时联网下载（如 nltk 数据缺失触发 nltk.download，"
                "网络不可达时无超时挂死，由 _download_nltk_data 预置数据规避）；"
                "或杀毒软件长时间扫描大体积 DLL。请把日志发回分析。"
            ) from e
        if proc.returncode != 0:
            self._dlog.error(
                "[冒烟] 失败（退出码 %s），输出末尾：\n%s",
                proc.returncode, self._output_tail(proc),
            )
        else:
            self._dlog.info("[冒烟] 通过（退出码 0）")
        return proc

    @staticmethod
    def _output_tail(proc: subprocess.CompletedProcess) -> str:
        """子进程输出的尾部片段（错误信息展示用）。"""
        return (proc.stderr or proc.stdout or "").strip()[-600:]

    @staticmethod
    def _looks_like_dll_failure(output: str) -> bool:
        """输出是否命中 DLL 加载类失败模式（见 _DLL_FAILURE_RE 说明）。"""
        return _DLL_FAILURE_RE.search(output) is not None

    def _wait_cancelable(self, seconds: int, reason: str) -> None:
        """可取消的等待（每秒检查一次取消标志）。"""
        dlog = self._dlog
        dlog.info("等待 %d 秒（%s）…", seconds, reason)
        for _ in range(seconds):
            if self._cancel.is_set():
                raise _Cancelled()
            time.sleep(1)

    def _smoke_with_dll_retry(
        self, python: Path, script: str, repair=None
    ) -> subprocess.CompletedProcess:
        """带 DLL 失败退避重试的冒烟测试（repair 为无参修复回调，可空）。

        WinError 1114 等 DLL 加载失败常见于杀毒软件实时扫描新释放的
        DLL：torch 一两百 MB 级的 DLL 在云主机/慢盘上扫描可持续数分钟，
        扫描期间 DllMain 被阻塞，安装完立刻导入必失败，稍后重试即通过
        （实测同机晚几分钟重跑成功）。重试策略：等 30s 复测 → repair
        （强制重装 torch）→ 等 15s 复测 → 等 60s 最后复测；非 DLL 类
        失败不重试，原样返回由调用方按具体错误上报。
        """
        dlog = self._dlog
        proc = self._smoke_run(python, script)
        if proc.returncode == 0 or not self._looks_like_dll_failure(
            self._output_tail(proc)
        ):
            return proc
        dlog.warning(
            "[冒烟] 疑似 DLL 加载失败（杀软扫描新释放 DLL 的典型表现），"
            "等待 30 秒后重试"
        )
        self._wait_cancelable(30, "等待杀软完成扫描")
        proc = self._smoke_run(python, script)
        if proc.returncode == 0 or not self._looks_like_dll_failure(
            self._output_tail(proc)
        ):
            return proc
        if repair is not None:
            dlog.warning("[冒烟] 等待重试仍失败，执行修复动作后复验")
            repair()
            self._wait_cancelable(15, "等待杀软扫描重装后的 DLL")
            proc = self._smoke_run(python, script)
            if proc.returncode == 0 or not self._looks_like_dll_failure(
                self._output_tail(proc)
            ):
                return proc
        dlog.warning(
            "[冒烟] 仍失败，再等 60 秒做最后一次复验"
            "（杀软扫描大体积 DLL 可能需要数分钟）"
        )
        self._wait_cancelable(60, "最后等待窗口")
        return self._smoke_run(python, script)

    def _repair_crashed_import(
        self,
        proc: subprocess.CompletedProcess,
        script: str,
        python: Path,
        site_packages: Path,
    ) -> subprocess.CompletedProcess:
        """冒烟以访问冲突（0xC0000005）退出时的自愈：强制重装崩住的包
        后复验一次，返回复验结果。

        崩溃进程无 traceback，逐模块 [import] 输出的最后一个模块即
        崩点（import 该模块时加载其 C 扩展 .pyd 死亡）。典型成因：
        旧轮次磁盘满时 pip 解压装坏的包，或杀软注入冲突。重装钉住
        现场已装版本（读 dist-info 目录名，防版本漂移），--no-deps +
        --force-reinstall --no-cache-dir（防缓存 wheel 同样损坏）。
        """
        dlog = self._dlog
        last_mod = ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith("[import] "):
                last_mod = line[len("[import] "):].strip()
        if not last_mod:
            return proc  # 无逐模块输出，无从定位，原样交回调用方上报
        project = _IMPORT_PKG_OVERRIDES.get(last_mod, last_mod)
        # dist-info 目录名的包名部分规范为下划线
        # （descript-audiotools → descript_audiotools-0.7.2.dist-info）
        norm = project.replace("-", "_")
        spec = None
        for d in site_packages.glob(f"{norm}-*.dist-info"):
            spec = f"{project}=={d.name[len(norm) + 1: -len('.dist-info')]}"
            break
        dlog.warning("[冒烟] 访问冲突（0xC0000005），崩点模块 %s", last_mod)
        # 1) 独立进程单测崩点模块：区分"组合加载暂态冲突"（单测通过→
        #    直接重跑完整冒烟即可，无需重装）与"模块本身在本机不可用"
        #    （单测也崩→逐候选版本重装再单测）。实测云主机 sentencepiece
        #    重装同版本仍崩（deploy 日志 2026-10-19）——文件无损坏，是
        #    该机环境对特定 .pyd 初始化的系统性拦截，需降版本绕开
        if self._import_probe(python, last_mod).returncode == 0:
            dlog.warning(
                "[冒烟] %s 单测通过（组合加载暂态冲突），重跑完整冒烟…",
                last_mod,
            )
            return self._smoke_with_dll_retry(python, script)
        candidates: list[str] = [spec] if spec else []
        for ver in _PKG_FALLBACK_VERSIONS.get(project, ()):
            cand = f"{project}=={ver}"
            if cand not in candidates:
                candidates.append(cand)
        # vendored 模块（third_party/index-tts，非 pip 安装）崩溃：无包可
        # 重装/回退，链上其余依赖此前均已逐一 import 通过，指向系统层
        # 对特定 .pyd 的拦截——原样交回调用方报指引文案
        if not candidates:
            dlog.error(
                "[冒烟] %s 在本机加载崩溃（访问冲突，且无 pip 候选版本可"
                "重试——该模块为内置源码 import 链，其依赖均已单独加载"
                "通过）。", last_mod,
            )
            return proc
        for cand in candidates:
            dlog.warning("[冒烟] 尝试安装 %s …", cand)
            try:
                self._pip_install(
                    [cand], tr("env.step.repair"), no_deps=True, python=python,
                    extra_args=["--force-reinstall", "--no-cache-dir"],
                )
            except RuntimeError as e:
                dlog.warning("[冒烟] %s 安装失败：%s", cand, e)
                continue
            if self._import_probe(python, last_mod).returncode == 0:
                dlog.info("[冒烟] %s 单测通过，重跑完整冒烟…", cand)
                return self._smoke_with_dll_retry(python, script)
        dlog.error(
            "[冒烟] %s 全部候选版本均在本机加载崩溃（访问冲突）。常见"
            "原因：安全软件 / 系统防护拦截 .pyd 初始化——请把安装"
            "目录加入其信任/排除列表后重试部署。", last_mod,
        )
        return proc

    def _import_probe(
        self, python: Path, module: str, timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        """独立进程单测 import 单个模块（访问冲突自愈的诊断/验证）。"""
        try:
            return subprocess.run(
                [str(python), "-I", "-c", f"import {module}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
                cwd=str(AppConfig.DATA_ROOT), env=sanitized_subprocess_env(),
                creationflags=_CREATION_FLAGS,
            )
        except subprocess.TimeoutExpired as e:
            # text=True 下 TimeoutExpired.stdout/stderr 仍可能是 bytes
            out = e.stdout or ""
            err = e.stderr or ""
            return subprocess.CompletedProcess(
                [str(python)], 124,
                stdout=out.decode("utf-8", "replace") if isinstance(out, bytes) else out,
                stderr=err.decode("utf-8", "replace") if isinstance(err, bytes) else err,
            )

    def _create_venv(
        self,
        venv_dir: Path,
        venv_python: Path,
        step_text: str,
        *,
        tag: str,
        system_site_packages: bool = False,
    ) -> None:
        """创建虚拟环境（幂等；ensurepip 失败时自诊断并清空重试一次）。

        venv 内部失败只报一行 Error——ensurepip 子进程的真实输出被 venv
        吞掉（实测独显机 2026-10-20 全新安装 ensurepip 退出码 1、零输出，
        无从定位）。故：
        1. 半残目录（有目录无 python.exe）先移入 .trash——venv 的升级
           路径在残缺目录上行为未定义；
        2. 创建前探测基础解释器 ensurepip 可用（快）——运行时损坏时
           提前给出明确报错，而不是 venv 深处的裸退出码；
        3. 创建失败时用 venv 的 python 直接重跑 ensurepip（--verbose）
           捕获真实报错写入部署日志，随后清空目录重试一次（安全软件
           瞬时锁定类失败可自愈）；重试仍失败才上报。
        """
        dlog = self._dlog
        if venv_python.is_file():
            dlog.info("[%s] %s 已存在，跳过创建：%s", tag, venv_dir.name, venv_python)
            return
        if venv_dir.exists():
            dlog.warning(
                "[%s] %s 目录残缺（无 python.exe），移入回收目录后重建：%s",
                tag, venv_dir.name, venv_dir,
            )
            self._kill_stale_venv_pythons()
            move_to_trash(venv_dir)
        base_py = _bootstrap_python()
        # 探测基础解释器 ensurepip：venv 创建的最后一步就是在新建 venv
        # 里跑 ensurepip 装入捆绑 pip，基础解释器缺 ensurepip/捆绑轮子
        # 时该步必然失败且无输出（runtime/python 打包不完整即此症状）
        dlog.info("[%s] 探测基础解释器 ensurepip：%s", tag, base_py)
        try:
            self._run([str(base_py), "-m", "ensurepip", "--version"])
        except RuntimeError as e:
            raise RuntimeError(
                f"基础解释器缺少可用的 ensurepip（内置运行时不完整），"
                f"无法创建虚拟环境：\n{e}"
            ) from e
        cmd = [str(base_py), "-m", "venv"]
        if system_site_packages:
            cmd.append("--system-site-packages")
        cmd.append(str(venv_dir))

        def _diagnose(stage: str) -> None:
            # venv 吞掉了 ensurepip 的真实输出：用 venv 的 python 直接
            # 重跑同一命令捕获完整报错（仅诊断，失败不上抛）
            if not venv_python.is_file():
                dlog.error("[%s] %s：venv 内无 python.exe 可诊断", tag, stage)
                return
            try:
                self._run(
                    [str(venv_python), "-m", "ensurepip",
                     "--upgrade", "--default-pip", "--verbose"],
                    step_text=None,
                )
            except RuntimeError as e:
                dlog.error("[%s] %s ensurepip 诊断输出：\n%s", tag, stage, e)

        self.step_changed.emit(step_text)
        try:
            self._run(cmd)
            return
        except RuntimeError as first_err:
            dlog.warning("[%s] venv 创建失败，诊断后清空重试一次：%s", tag, first_err)
        _diagnose("首次创建失败")
        self._kill_stale_venv_pythons()
        try:
            move_to_trash(venv_dir)
        except OSError as e:
            raise RuntimeError(
                f"venv 创建失败且残缺目录无法移入回收目录"
                f"（可能有程序占用，请重启软件后重试）：\n{e}"
            ) from e
        try:
            self._run(cmd)
        except RuntimeError as retry_err:
            _diagnose("重试仍失败")
            raise RuntimeError(
                f"venv 重建重试仍失败（真实报错已写入部署日志）：\n{retry_err}"
            ) from retry_err

    def _kill_stale_venv_pythons(self) -> None:
        """结束两个子进程 venv 内的残留 python 进程（幂等、可失败）。

        残留的 ml/tts worker 会锁定 venv 内 DLL（如 sklearn 的
        msvcp140.dll），pip 重装覆盖时报 Permission denied 并留下半装
        包（deploy 日志 2026-10-19 INTEL 机）。部署属维护动作，此时
        不应有任务在跑，结束残留进程是安全的；找不到任何进程时静默。
        """
        if os.name != "nt":
            return
        prefixes = (str(ML_VENV_DIR), str(TTS_VENV_DIR))
        query = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", query],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_CREATION_FLAGS,
            ).stdout
        except (OSError, subprocess.SubprocessError) as e:
            self._dlog.warning("[部署] 扫描残留进程失败（忽略）：%s", e)
            return
        killed = 0
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid_s, _, exe_path = line.partition("|")
            if not exe_path:
                continue
            if not any(
                exe_path.lower().startswith(p.lower()) for p in prefixes
            ):
                continue
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid_s],
                    capture_output=True,
                    timeout=15,
                    creationflags=_CREATION_FLAGS,
                )
                killed += 1
                self._dlog.info(
                    "[部署] 已结束残留 venv 进程 PID %s：%s", pid_s, exe_path
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if killed:
            self._dlog.info(
                "[部署] 共结束 %d 个残留 venv 进程，等待 5 秒句柄释放…",
                killed,
            )
            for _ in range(5):
                if self._cancel.is_set():
                    raise _Cancelled()
                time.sleep(1)

    def _repair_locked_package(self, err_text: str, python: Path) -> None:
        """从 pip 报错中定位被锁的包并强制重装（修复半装状态）。

        pip 换源重跑会把半装包（dist-info 已写入）按 already satisfied
        跳过，坏包潜伏到冒烟/运行期才爆。site-packages 目录名与 PyPI
        包名不一致的少数包在此映射（sklearn→scikit-learn 等）。
        """
        import re as _re

        m = _re.search(r"site-packages[\\\\/]+([A-Za-z0-9_.-]+)", err_text)
        if m is None:
            return
        seg = m.group(1)
        pip_name = {
            "sklearn": "scikit-learn",
            "yaml": "pyyaml",
            "PIL": "pillow",
            "cv2": "opencv-python",
        }.get(seg, seg)
        pinned = next(
            (
                spec[len(pip_name) + 2:]
                for lst in (_ML_PACKAGES, _SUB_DEPS, [_ML_TORCH])
                for spec in lst
                if spec.startswith(pip_name + "==")
            ),
            None,
        )
        target = f"{pip_name}=={pinned}" if pinned else pip_name
        # 等待锁释放（安全软件扫描通常数十秒内结束），期间可取消
        for _ in range(25):
            if self._cancel.is_set():
                raise _Cancelled()
            time.sleep(1)
        self._dlog.info(
            "[pip] 定位到被占用包 %s（目录名 %s），强制重装 %s",
            pip_name, seg, target,
        )
        cmd = [
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--force-reinstall", "--no-deps", target,
        ]
        try:
            self._run(cmd, f"修复 {pip_name}")
        except RuntimeError as e:
            self._dlog.warning(
                "[pip] 修复 %s 未成功（继续重试主安装）：%s", pip_name, e
            )

    def _pip_install(
        self,
        packages: list[str],
        step_text: str,
        no_deps: bool,
        python: Path | None = None,
        constraints: Path | None = None,
        extra_args: list[str] | None = None,
        indexes: list[str] | None = None,
    ) -> None:
        """在指定虚拟环境内执行 pip install（默认 .venv-tts）。

        constraints 提供时用 -c 锁定共享依赖版本（防 pip 重解析 torch）；
        extra_args 追加 pip 选项（如 --force-reinstall --no-cache-dir）；
        indexes 覆盖默认下载源（如 CUDA torch 用 _TORCH_CU_INDEX 单源）。

        下载源按给定列表（默认 _PIP_INDEXES：国内镜像优先、官方源兜底）
        逐个尝试：单源下载失败（限速中断 / 连接重置 / 镜像缺包）自动换
        下一个源整体重跑。

        TMP/TEMP 重定向到 DATA_ROOT/.pip-tmp：pip 下载/解包中转大轮子
        （CUDA torch 约 2.9GB）默认写系统盘 %TEMP%，系统盘紧张直接
        Errno 28（实测 2026-10-19 独显机 C: 仅剩 2GB，CUDA 轮子下到
        1.6GB 即失败回退 CPU 版）——中转落数据盘后不再受系统盘限制。

        安装被文件占用打断（Permission denied / Errno 13 / WinError 5，
        多为安全软件扫描新释放的 DLL 或残留进程锁定）时不换源：等待
        25 秒修复被锁包后原源重试（至多 2 次）——直接换源重跑会把
        半装包按 already satisfied 跳过，坏包潜伏到冒烟/运行期才爆
        （实测 2026-10-19 INTEL 机 sklearn 半装，TTS 经 .pth 共享
        踩雷才报出误导性的错）。
        """
        py = python or get_tts_venv_python()
        pip_tmp = AppConfig.DATA_ROOT / ".pip-tmp"
        pip_tmp.mkdir(parents=True, exist_ok=True)
        # PYTHONNOUSERSITE：让 pip 的"已安装"判定视野与 -I 冒烟/运行态
        # 一致。用户级 site-packages（%APPDATA%\Python\Py3xx\site-packages）
        # 对 venv 解释器可见，其中已装的包会被 pip 误判 already satisfied
        # 跳装（matplotlib 依赖 pillow/contourpy 等即因此漏进不了 venv，
        # 部署日志 2026-10-30 冒烟 No module named 'PIL'），而 -I 冒烟
        # 恰好看不到用户级目录——闭包缺口潜伏到冒烟期才爆。干净部署机
        # 无用户级包不受影响，属开发机特有的"本机已装"参照污染
        env_extra = {
            "TMP": str(pip_tmp), "TEMP": str(pip_tmp),
            "PYTHONNOUSERSITE": "1",
        }
        sources = list(indexes) if indexes is not None else list(_PIP_INDEXES)
        last_err: RuntimeError | None = None
        attempt = 0
        lock_repairs = 0
        while attempt < len(sources):
            index = sources[attempt]
            if attempt > 0:
                self._dlog.warning(
                    "[pip] 上一下载源失败，换源重试（%d/%d）：%s",
                    attempt + 1, len(sources), index or "官方默认源",
                )
            cmd = [
                str(py), "-m", "pip", "install", "--disable-pip-version-check",
            ]
            if index:
                cmd += ["-i", index]
            if constraints is not None:
                cmd += ["-c", str(constraints)]
            if no_deps:
                cmd.append("--no-deps")
            if extra_args:
                cmd.extend(extra_args)
            cmd.extend(packages)
            shown = (
                step_text if attempt == 0
                else tr("env.step.pip_retry", step=step_text)
            )
            try:
                self._run(cmd, shown, env_extra=env_extra)
                return
            except _Cancelled:
                raise
            except RuntimeError as e:
                last_err = e
                msg = str(e)
                if (
                    lock_repairs < 2
                    and ("Permission denied" in msg or "Errno 13" in msg
                         or "拒绝访问" in msg or "WinError 5" in msg)
                ):
                    lock_repairs += 1
                    self._dlog.warning(
                        "[pip] 安装被文件占用打断（第 %d/2 次修复，"
                        "等待 25 秒后原源重试）", lock_repairs,
                    )
                    self._repair_locked_package(msg, py)
                    continue  # 不换源：先修复半装包再原源重跑
                attempt += 1
        assert last_err is not None
        raise last_err

    def _run(
        self,
        cmd: list[str],
        step_text: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        """执行子进程，stdout 按行回流为详细日志；非零退出码抛 RuntimeError。

        子进程环境经净化（见 _smoke_run 说明），杜绝随包 DLL 干扰；
        env_extra 在净化基础上追加覆盖（如 pip 的 TMP/TEMP 重定向）。
        完整命令与全部输出行写入部署日志；失败时错误信息附带完整命令
        与输出末尾，弹窗中即可直接定位原因（不再截断为前 6 个参数）。
        """
        if step_text:
            self.step_changed.emit(step_text)
        shown_cmd = " ".join(str(c) for c in cmd)
        if step_text:
            self._dlog.info("[步骤] %s", step_text)
        self._dlog.info("[命令] %s", shown_cmd)
        proc_env = sanitized_subprocess_env()
        if env_extra:
            proc_env.update(env_extra)
        try:
            self._proc = subprocess.Popen(
                [str(c) for c in cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(AppConfig.DATA_ROOT),
                creationflags=_CREATION_FLAGS,
                env=proc_env,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"未找到可执行程序（{cmd[0]}）：{e}") from e
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        tail: deque[str] = deque(maxlen=12)
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.detail_line.emit(line)
                self._dlog.info("[输出] %s", line)
                tail.append(line)
        returncode = proc.wait()
        self._proc = None
        if self._cancel.is_set():
            raise _Cancelled()
        if returncode != 0:
            tail_text = "\n".join(tail)
            self._dlog.error(
                "[失败] 退出码 %s，输出末尾：\n%s", returncode, tail_text
            )
            raise RuntimeError(
                f"命令执行失败（退出码 {returncode}）：\n{shown_cmd}\n"
                f"—— 输出末尾 ——\n{tail_text}"
            )


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
        ok = (
            bool(results.get("ml_ready"))
            and bool(results.get("tts_ready"))
            and bool(results.get("llama_ready"))
        )
        detail = (
            f"{tr('bar.env_ml')}：{_status_text(results.get('ml_ready'))}；"
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
