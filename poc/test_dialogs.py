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
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit  # noqa: E402

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
    dlg = AsrReviewDialog(segs, str(TEST_AUDIO))
    check("Asr 表格行数=8", dlg._table.rowCount() == 8)
    combo = dlg._table.cellWidget(0, 3)
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
    dlg._toggle_delete_state(2)
    check("Asr 删除槽置 deleted=True", segs[2].deleted is True)
    check("Asr 状态列文本=已标记删除", dlg._table.item(2, 5).text() == "已标记删除")
    check("Asr 操作按钮变“恢复”", dlg._action_buttons[2].text() == "恢复")
    check("Asr 置灰后说话人下拉禁用", not dlg._table.cellWidget(2, 3).isEnabled())
    check("Asr 统计更新为已删除 2 句", "已标记删除 2 句" in dlg._stats_label.text())
    dlg._toggle_delete_state(2)
    check("Asr 再次调用恢复 deleted=False", segs[2].deleted is False)
    check("Asr 恢复后状态列文本=正常", dlg._table.item(2, 5).text() == "正常")
    check("Asr 恢复后按钮变“删除”", dlg._action_buttons[2].text() == "删除")
    check("Asr 初始 deleted 行(4)按钮为“恢复”", dlg._action_buttons[4].text() == "恢复")
    check("Asr 初始 deleted 行状态文本", dlg._table.item(4, 5).text() == "已标记删除")
    # 修改说话人 / 文本 → get_result 反映变更
    dlg._table.cellWidget(3, 3).setCurrentText("S02")
    check("Asr 修改说话人回写 spk=S02", segs[3].spk == "S02")
    dlg._table.cellWidget(0, 4).setText("Hello everyone, welcome back.")
    check("Asr 修改文本回写", segs[0].text == "Hello everyone, welcome back.")
    check("Asr get_result 返回同一列表", dlg.get_result() is segs)
    # 选中行 → ffmpeg 切片段 → 底部加载试听
    dlg._table.selectRow(1)
    check("Asr 选中行后切片段加载试听",
          dlg._player._source != "" and Path(dlg._player._source).is_file())

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
    review = [segs[0], segs[1]]  # needs_review 的 2 句
    rdlg = TtsReviewDialog(review)
    check("Tts 表格行数=2", rdlg._table.rowCount() == 2)
    check("Tts 伸缩比显示 1.45x/0.85x",
          rdlg._table.item(0, 4).text() == "1.45x" and rdlg._table.item(1, 4).text() == "0.85x")
    cb0 = rdlg._table.cellWidget(0, 6)
    cb1 = rdlg._table.cellWidget(1, 6)
    check("Tts cellWidget 为 QCheckBox", isinstance(cb0, QCheckBox) and isinstance(cb1, QCheckBox))
    if isinstance(cb0, QCheckBox):
        cb0.setChecked(True)
    if isinstance(cb1, QCheckBox):
        cb1.setChecked(True)
    check("Tts 勾选两条 indices=[0,1]", rdlg.get_regenerate_indices() == [0, 1])
    check("Tts 统计标签", "已勾选重生成 2 句" in rdlg._stats_label.text())
    rdlg._select_all_button.click()  # 已全选 → 切换为全不选
    check("Tts 全部重生成切换为全不选", rdlg.get_regenerate_indices() == [])
    rdlg._select_all_button.click()  # 再点 → 全选
    check("Tts 全部重生成再次全选", rdlg.get_regenerate_indices() == [0, 1])
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
