# -*- coding: utf-8 -*-
"""资源管理器与下载器验证脚本。

覆盖：
1) refresh_all() 真实状态检测：FFmpeg/ASR/LLM/Demucs 已就绪（本机模型/缓存齐全），TTS 未就绪
2) LLM 档位切换：set_llm_variant 切到 qwen3-1.7b 后路径变化、切回 qwen3-4b 恢复就绪
3) ModelDownloader 冒烟：从魔摆下载 Qwen/Qwen3-0.6B-GGUF 的 README（*.md）到临时目录，
   断言收到 download_finished / download_progress 且文件存在；同 key 重复下载被忽略
4) delete_resource / move_to_trash 冒烟：自定义 SUBDUB_MODELS_ROOT 后删除假模型目录

运行（offscreen）：.venv\\Scripts\\python.exe -B poc\\test_resources.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("modelscope").setLevel(logging.WARNING)

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

from app.resources.downloader import ModelDownloader  # noqa: E402
from app.resources.model import (  # noqa: E402
    LLM_VARIANTS,
    RESOURCE_META,
    get_models_root,
    get_resource_manager,
    move_to_trash,
)


def phase1_status() -> None:
    """真实状态检测快照。"""
    manager = get_resource_manager()
    manager.refresh_all()
    print("=== 资源状态快照（models root: %s）===" % get_models_root())
    for info in manager.resources():
        print(
            f"  {info.key:12s} {info.model_name:26s} "
            f"{'READY' if info.status.is_ready else 'NOT_READY':10s} "
            f"size={info.download_size_gb}GB path={info.local_path}"
        )

    assert manager.get("ffmpeg").status.is_ready, "FFmpeg 应已就绪（内置 libs）"
    assert manager.get("transcribe").status.is_ready, "ASR 应已就绪（models/asr 有模型）"
    assert manager.get("translate").status.is_ready, "LLM 应已就绪（models/llm 有 gguf）"
    assert manager.get("demucs").status.is_ready, "Demucs 应已就绪（本机 torch hub 已缓存）"
    assert not manager.get("tts").status.is_ready, "TTS 应未就绪（存在 gpt.pth.incomplete 半成品）"

    assert manager.get_resource_path("translate"), "get_resource_path(translate) 应有值"
    assert manager.get_resource_path("tts") is None, "get_resource_path(tts) 应为 None"
    print("[PASS] phase1 状态检测")


def phase2_llm_variant() -> None:
    """LLM 档位切换。"""
    manager = get_resource_manager()
    manager.set_llm_variant("qwen3-1.7b")
    assert RESOURCE_META["translate"].repo_id == "Qwen/Qwen3-1.7B-GGUF"
    assert manager.get("translate").model_name == "Qwen3-1.7B"
    assert not manager.get("translate").status.is_ready, "本地无 1.7b 模型，应未就绪"
    assert manager.get_resource_path("translate") is None

    manager.set_llm_variant("qwen3-4b")
    assert RESOURCE_META["translate"].default_subdir == "llm/Qwen3-4B-GGUF"
    assert manager.get("translate").status.is_ready, "切回 qwen3-4b 应恢复就绪"

    try:
        manager.set_llm_variant("bogus")
        raise AssertionError("未知档位应抛 ValueError")
    except ValueError:
        pass
    print("[PASS] phase2 LLM 档位（可选档位: %s）" % sorted(LLM_VARIANTS))


def phase3_downloader(app: QCoreApplication) -> None:
    """下载器冒烟：小仓库 + 只下载 README。"""
    downloader = ModelDownloader()
    events: dict[str, list] = {"progress": [], "finished": [], "failed": []}
    downloader.download_progress.connect(lambda k, p, s: events["progress"].append((k, p, s)))
    downloader.download_finished.connect(lambda k, p: events["finished"].append((k, p)))
    downloader.download_failed.connect(lambda k, e: events["failed"].append((k, e)))

    tmp = tempfile.mkdtemp(prefix="subdub_smoke_")
    target = Path(tmp) / "Qwen3-0.6B-GGUF"
    downloader.start_download(
        "smoke_test", "Qwen/Qwen3-0.6B-GGUF", str(target),
        allow_patterns=["*.md"], total_size_gb=0.001,  # 约 1MB 预估值，仅用于百分比换算
    )
    assert downloader.is_downloading("smoke_test"), "下载应正在进行"

    # 同 key 重复下载：应被忽略（仍是同一线程对象）
    first_thread = downloader._threads["smoke_test"]
    downloader.start_download("smoke_test", "Qwen/Qwen3-0.6B-GGUF", str(target), allow_patterns=["*.md"])
    assert downloader._threads["smoke_test"] is first_thread, "重复下载应被忽略"

    loop = QEventLoop()
    QTimer.singleShot(180_000, loop.quit)  # 超时保护（含 modelscope 导入时间）
    downloader.download_finished.connect(lambda *a: loop.quit())
    downloader.download_failed.connect(lambda *a: loop.quit())
    loop.exec()

    assert not events["failed"], f"下载失败: {events['failed']}"
    assert events["finished"], "未收到 download_finished"
    assert events["progress"], "未收到 download_progress"
    assert target.is_dir() and any(target.rglob("*.md")), f"README 未落盘: {target}"
    app.processEvents()
    print(f"[PASS] phase3 下载器冒烟：finished={events['finished'][0][1]}")
    print(f"       progress 共 {len(events['progress'])} 次，最后一次: {events['progress'][-1]}")


def phase4_delete() -> None:
    """move_to_trash 与 delete_resource 冒烟（自定义模型根目录，不碰真实模型）。"""
    # 4a) move_to_trash 直接验证
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "some_model"
        d.mkdir()
        (d / "model.gguf").write_bytes(b"x" * 1024)
        moved = move_to_trash(d)
        assert not d.exists() and moved.exists(), "move_to_trash 应移动目录"
        assert moved.parent.name == ".trash", "应移入 .trash 子目录"

    # 4b) delete_resource：SUBDUB_MODELS_ROOT 指向临时根
    tmp_root = tempfile.mkdtemp(prefix="subdub_models_root_")
    os.environ["SUBDUB_MODELS_ROOT"] = tmp_root
    try:
        manager = get_resource_manager()
        manager.refresh_all()
        assert not manager.get("translate").status.is_ready, "自定义根下无模型，应未就绪"

        fake_dir = Path(tmp_root) / "llm" / "Qwen3-4B-GGUF"
        fake_dir.mkdir(parents=True)
        (fake_dir / "Qwen3-4B-Q4_K_M.gguf").write_bytes(b"x" * 2048)  # 小文件，仅验证删除逻辑
        manager.delete_resource("translate")
        assert not fake_dir.exists(), "delete_resource 后目录应不存在"
        assert (Path(tmp_root) / "llm" / ".trash").is_dir(), "删除内容应进入 .trash"

        manager.delete_resource("ffmpeg")  # 内置工具：仅 warning，不抛异常
        assert manager.get("ffmpeg").status.is_ready, "FFmpeg 不应被删除"
    finally:
        del os.environ["SUBDUB_MODELS_ROOT"]

    manager.refresh_all()
    assert manager.get("translate").status.is_ready, "恢复真实根后 LLM 应重新就绪"
    print("[PASS] phase4 删除与自定义模型根目录（SUBDUB_MODELS_ROOT）")


def main() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    phase1_status()
    phase2_llm_variant()
    phase3_downloader(app)
    phase4_delete()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
