# -*- coding: utf-8 -*-
"""四个审核/管理对话框的离屏（offscreen）验证脚本。

- 使用自建 FakeSegment（鸭子类型，字段与 app.core.segment.Segment 一致），
  不 import 真实 Segment，避免依赖其他代理编写中的模块。
- 不启动 exec() 事件循环：构造对话框 → 直接调用方法/槽 → 断言状态。
- 用 ffmpeg 从 poc/test_audio.wav 切出两个片段，模拟 TTS 生成音频。
"""

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.core.i18n as i18n  # noqa: E402

i18n._current_language = "zh-CN"  # 断言基于中文文案：仅改内存语言，不写用户设置

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton  # noqa: E402

from app.core.ffmpeg import get_ffmpeg_path  # noqa: E402
from app.ui.style import APPLICATION_QSS  # noqa: E402
from app.ui.widgets.audio_player import AudioPlayerBar  # noqa: E402
from app.ui.dialogs.asr_review_dialog import AsrReviewDialog  # noqa: E402
from app.ui.dialogs.translation_review_dialog import TranslationReviewDialog  # noqa: E402
from app.ui.dialogs.voice_manage_dialog import VoiceManageDialog  # noqa: E402
from app.ui.dialogs.tts_review_dialog import TtsReviewDialog  # noqa: E402

TEST_AUDIO = ROOT / "poc" / "test_audio.wav"


@dataclass
class FakeSegment:
    """与 app.core.segment.Segment 字段一致的最小鸭子类型实现。"""

    spk: str = "S01"
    start: float = 0.0
    end: float = 0.0
    text: str = ""
    target_text: str = ""
    deleted: bool = False
    needs_review: bool = False
    tts_audio_path: str = ""
    stretch_ratio: float = 1.0

    @property
    def duration(self) -> float:
        return self.end - self.start


def make_tts_clip(start: float, end: float, name: str) -> str:
    """用 ffmpeg 从 test_audio.wav 切片段，模拟 TTS 生成的配音文件。"""
    ffmpeg = get_ffmpeg_path()
    out = str(Path(tempfile.gettempdir()) / name)
    result = subprocess.run(
        [ffmpeg, "-ss", str(start), "-i", str(TEST_AUDIO), "-t", str(end - start), "-y", out],
        capture_output=True,
    )
    assert result.returncode == 0 and Path(out).is_file(), f"切片段失败: {name}"
    return out


def build_segments(clip_a: str, clip_b: str) -> list:
    """8 条测试数据：2 说话人、1 条 deleted、2 条 needs_review 带 tts 音频、时间重叠。"""
    return [
        FakeSegment(spk="S01", start=0.00, end=2.50, text="Hello, welcome to our channel.",
                    target_text="你好，欢迎来到我们的频道。", needs_review=True,
                    tts_audio_path=clip_a, stretch_ratio=1.45),
        FakeSegment(spk="S02", start=2.60, end=5.10, text="Today we will talk about video dubbing.",
                    target_text="今天我们要一起好好聊聊视频配音这件事。", needs_review=True,
                    tts_audio_path=clip_b, stretch_ratio=0.85),
        # 与上一句时间重叠（4.80 < 5.10）
        FakeSegment(spk="S01", start=4.80, end=7.20, text="Let's get started.",
                    target_text="我们开始吧。"),
        FakeSegment(spk="S02", start=7.30, end=9.00, text="First, separate the vocals.",
                    target_text="首先，分离出人声。"),
        # 无意义内容，标记删除
        FakeSegment(spk="S01", start=1.00, end=3.00, text="Um... uh...", deleted=True),
        # 与第 0 句时间重叠（0.50 < 2.50）
        FakeSegment(spk="S02", start=0.50, end=1.80, text="OK.", target_text="好的。"),
        FakeSegment(spk="S01", start=3.00, end=4.00, text="Check the audio.",
                    target_text="检查音频。"),
        FakeSegment(spk="S02", start=6.00, end=8.50, text="That's all for the intro.",
                    target_text="以上就是开场部分。"),
    ]


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(APPLICATION_QSS)

    clip_a = make_tts_clip(0.0, 2.5, "subdub_test_tts_a.wav")
    clip_b = make_tts_clip(2.6, 5.1, "subdub_test_tts_b.wav")
    # 重生成后的新文件（模拟译文改变 → _tts_out 命名变化）
    clip_regen = make_tts_clip(0.0, 2.5, "subdub_test_tts_regen.wav")
    segs = build_segments(clip_a, clip_b)

    checks = []

    def check(name: str, cond) -> None:
        ok = bool(cond)
        checks.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")

    # ---------------------------------------------------------------- 1. AudioPlayerBar 冒烟
    bar = AudioPlayerBar()
    check("Player 无源时按钮禁用", not bar._play_button.isEnabled())
    bar.set_source(str(TEST_AUDIO))
    check("Player 加载后按钮启用", bar._play_button.isEnabled())
    try:
        bar.play()
        bar.stop()
        check("Player play/stop 不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check(f"Player play/stop 不抛异常（{exc!r}）", False)
    bar.set_source("")
    check("Player 清空源后重新禁用", not bar._play_button.isEnabled())

    # ---------------------------------------------------------------- 2. AsrReviewDialog
    # 表格/试听条已抽取为共用控件 SegmentsTableWidget（dlg._table_widget）
    dlg = AsrReviewDialog(segs, str(TEST_AUDIO))
    tbl = dlg._table_widget._table
    check("Asr 表格行数=8", tbl.rowCount() == 8)
    combo = tbl.cellWidget(0, 3)
    check("Asr cellWidget(0,3) 为 QComboBox", isinstance(combo, QComboBox))
    check(
        "Asr 说话人下拉选项=[S01,S02]",
        isinstance(combo, QComboBox)
        and [combo.itemText(i) for i in range(combo.count())] == ["S01", "S02"],
    )
    check(
        "Asr 统计标签",
        dlg._stats_label.text() == "共 8 句 / 已标记删除 1 句 / 说话人 2 人",
    )
    # 删除 → 置灰 → 恢复（直接调用内部槽）
    dlg._table_widget._toggle_delete_state(2)
    check("Asr 删除槽置 deleted=True", segs[2].deleted is True)
    check("Asr 状态列文本=已标记删除", tbl.item(2, 5).text() == "已标记删除")
    check("Asr 操作按钮变“恢复”", dlg._table_widget._action_buttons[2].text() == "恢复")
    check("Asr 置灰后说话人下拉禁用", not tbl.cellWidget(2, 3).isEnabled())
    check("Asr 统计更新为已删除 2 句", "已标记删除 2 句" in dlg._stats_label.text())
    dlg._table_widget._toggle_delete_state(2)
    check("Asr 再次调用恢复 deleted=False", segs[2].deleted is False)
    check("Asr 恢复后状态列文本=正常", tbl.item(2, 5).text() == "正常")
    check("Asr 恢复后按钮变“删除”", dlg._table_widget._action_buttons[2].text() == "删除")
    check("Asr 初始 deleted 行(4)按钮为“恢复”", dlg._table_widget._action_buttons[4].text() == "恢复")
    check("Asr 初始 deleted 行状态文本", tbl.item(4, 5).text() == "已标记删除")
    # 修改说话人 / 文本 → get_result 反映变更
    tbl.cellWidget(3, 3).setCurrentText("S02")
    check("Asr 修改说话人回写 spk=S02", segs[3].spk == "S02")
    tbl.cellWidget(0, 4).setText("Hello everyone, welcome back.")
    check("Asr 修改文本回写", segs[0].text == "Hello everyone, welcome back.")
    check("Asr get_result 返回同一列表", dlg.get_result() is segs)
    # 选中行 → ffmpeg 切片段 → 底部加载试听
    tbl.selectRow(1)
    player = dlg._table_widget._player
    check("Asr 选中行后切片段加载试听",
          player._source != "" and Path(player._source).is_file())

    # ---------------------------------------------------------------- 3. TranslationReviewDialog
    tdlg = TranslationReviewDialog(segs, "英语", "中文", str(TEST_AUDIO))
    check("Trans 表格行数=8", tdlg._table.rowCount() == 8)
    check(
        "Trans 统计标签",
        "共 8 句" in tdlg._stats_label.text() and "已标记删除 1 句" in tdlg._stats_label.text(),
    )
    check("Trans 匹配列 seg0=匹配", tdlg._table.item(0, 6).text() == "匹配")
    check("Trans 匹配列 seg1=偏长", tdlg._table.item(1, 6).text() == "偏长")
    check("Trans 匹配列 seg2=偏短", tdlg._table.item(2, 6).text() == "偏短")
    check("Trans deleted 行译文输入禁用", not tdlg._table.cellWidget(4, 4).isEnabled())
    check("Trans deleted 行匹配列=—", tdlg._table.item(4, 6).text() == "—")
    tdlg._table.cellWidget(2, 4).setText("好的好的好的好的好的好的好的好的")
    check("Trans 译文修改回写", segs[2].target_text == "好的好的好的好的好的好的好的好的")
    check("Trans 译文变长后匹配列=偏长", tdlg._table.item(2, 6).text() == "偏长")
    check("Trans get_result 返回同一列表", tdlg.get_result() is segs)
    # 选中行 → ffmpeg 切片段 → 底部加载试听
    tdlg._table.selectRow(3)
    check("Trans 选中行后切片段加载试听",
          tdlg._player._source != "" and Path(tdlg._player._source).is_file())
    # audio_wav 为空时禁用试听
    tdlg2 = TranslationReviewDialog(segs, "英语", "中文", "")
    check("Trans 无音轨时试听按钮禁用", not tdlg2._player._play_button.isEnabled())

    # ---------------------------------------------------------------- 4. VoiceManageDialog
    vdlg = VoiceManageDialog(segs, {"S01": clip_a, "S02": clip_b})
    check("Voice 卡片数=2", len(vdlg._cards) == 2)
    check("Voice S01 句数标签(3 句)", "S01（3 句）" in vdlg._cards["S01"]["name_label"].text())
    check("Voice S02 句数标签(4 句)", "S02（4 句）" in vdlg._cards["S02"]["name_label"].text())
    # 例句 = 该说话人未删除句子中 text 最长的一句
    s01_texts = [s.text for s in segs if s.spk == "S01" and not s.deleted and s.text]
    expected_sample = max(s01_texts, key=len)
    check("Voice 例句展示最长句",
          vdlg._cards["S01"]["sample_label"].text() == f"例句：{expected_sample}")
    vdlg.set_ref_path("S01", clip_b)
    check("Voice 更换参考路径生效", vdlg.get_ref_map()["S01"] == clip_b)
    check("Voice 未更换项保持原值", vdlg.get_ref_map()["S02"] == clip_b)
    try:
        vdlg._cards["S01"]["listen_button"].click()
        check("Voice 试听点击不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check(f"Voice 试听点击不抛异常（{exc!r}）", False)

    # ---------------------------------------------------------------- 5. TtsReviewDialog
    review = [segs[0], segs[1]]  # needs_review 的 2 句（伸缩比 1.45 / 0.85）
    regen_rows: list = []
    progress_seen: list = []

    def fake_regen_hook(row: int, progress=None) -> float:
        """模拟 regen_hook：重置伸缩比、换成新文件名（译文改变 → 文件名
        变化），记录行号与收到的进度回调。"""
        regen_rows.append(row)
        if progress is not None:
            progress_seen.append(progress)
            progress(0.5, "speech synthesis 1/1...")
        seg = review[row]
        seg.stretch_ratio = 1.2
        seg.needs_review = False
        seg.tts_audio_path = clip_regen  # 新命名文件，试听按钮须重链接
        return 1.2

    def wait_regen_done(dlg, timeout: float = 5.0) -> None:
        """processEvents 循环等待重生成队列排空（排队信号在主线程送达）。"""
        deadline = time.time() + timeout
        while dlg._regen_active and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

    rdlg = TtsReviewDialog(review, regen_hook=fake_regen_hook)
    check("Tts 表格行数=2", rdlg._table.rowCount() == 2)
    check("Tts 伸缩比显示 1.45x/0.85x",
          rdlg._table.item(0, 4).text() == "1.45x" and rdlg._table.item(1, 4).text() == "0.85x")
    btn0 = rdlg._table.cellWidget(0, 6)
    btn1 = rdlg._table.cellWidget(1, 6)
    check("Tts 第6列为重生成按钮",
          isinstance(btn0, QPushButton) and isinstance(btn1, QPushButton))
    check("Tts 兼容接口恒返回空列表", rdlg.get_regenerate_indices() == [])
    check("Tts 初始统计异常 2 句", "异常 2 句" in rdlg._stats_label.text())

    # 单句即时重生成：点击按钮 → 排队 → 后台执行 → 完成后按钮恢复
    btn0.click()
    check("Tts 单句点击后待重生成 1 句", "待重生成 1 句" in rdlg._stats_label.text())
    wait_regen_done(rdlg)
    check("Tts 单句重生成执行完成", not rdlg._regen_active and regen_rows == [0])
    check("Tts 重生成进度回调可用", len(progress_seen) == 1)
    check("Tts 重生成后伸缩比 1.20x", rdlg._table.item(0, 4).text() == "1.20x")
    check("Tts 重生成后按钮恢复", btn0.text() == "重新生成此句" and btn0.isEnabled())
    check("Tts 重生成后确认按钮恢复", rdlg._ok_button.isEnabled())
    rdlg._listen_buttons[0].click()
    check("Tts 试听按钮重链接新文件", rdlg._player._source == clip_regen)

    # 批量按钮 1：重生成所有异常句（第 1 句已正常，仅第 2 句入队）
    rdlg._regen_abnormal_button.click()
    check("Tts 批量异常句待重生成 1 句", "待重生成 1 句" in rdlg._stats_label.text())
    wait_regen_done(rdlg)
    check("Tts 批量异常句执行完成", regen_rows == [0, 1])

    # 批量按钮 2：重生成所选句（选中第 2 行）
    rdlg._table.selectRow(1)
    rdlg._regen_selected_button.click()
    wait_regen_done(rdlg)
    check("Tts 批量所选句执行完成", regen_rows == [0, 1, 1])
    check("Tts 全部正常后统计异常 0 句", "异常 0 句" in rdlg._stats_label.text())
    rdlg._table.cellWidget(0, 3).setText("重新生成用的修改译文")
    check("Tts 译文编辑回写", review[0].target_text == "重新生成用的修改译文")
    check("Tts get_edited_segments 返回同一列表", rdlg.get_edited_segments() is review)
    try:
        rdlg._listen_buttons[0].click()
        check("Tts 试听点击不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check(f"Tts 试听点击不抛异常（{exc!r}）", False)

    # ---------------------------------------------------------------- 汇总
    failed = [name for name, ok in checks if not ok]
    print(f"\n共 {len(checks)} 项检查：通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print(f"  失败: {name}")
    print("全部通过" if not failed else "存在失败项")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
