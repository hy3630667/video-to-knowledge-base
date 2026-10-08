# -*- coding: utf-8 -*-
"""
纠错链路的测试：分块 map-reduce + 校验闸门 + 规则清洗

背景（2026-10-08 真实事故）：
  64 分钟视频的转写稿 19093 字，一次性喂给纠错 LLM →
  「校对稿」一节就把 max_tokens 吃光，`## 知识点`/`## 问答对` 被截断
  （finish_reason=length）→ 合规策略又正确丢掉校对稿 → **入库只剩 105 字**，
  而流程打印「✅ 完成」并 exit 0。

参考实现（开源调研）：
  · 拾语 shiyu-ai-knowledge-base `backend/cleaner.py`
    - 分块 map-reduce：「避免整篇一次性总结丢信息、跑题」
    - `_split_text_at(text, 3500)`：按 3500 字、句子边界切
    - L244：「DeepSeek 默认思考模式会吃满 max_tokens，结构化 JSON 场景显式关掉」
  · learning-video-skills：闸门要「**真检查非存在性检查**」，且校验器要**双向有分辨力**

跑法：python -m unittest test_pipeline_correction -v
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("P", os.path.join(os.path.dirname(HERE), "scripts", "one-click-pipeline.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

# 完整的一块（三节齐全）
GOOD = """## 校对稿
今天我们讲三彩工作流的新版本。

## 知识点
- 新版三彩推翻上一版的二彩和一彩
- 二彩三彩生成时长更短、显存占用更低

## 问答对
Q: 新版和旧版差在哪？
A: 画面、声音、动作连贯性都改善了。
"""

# 被截断的一块：只有校对稿，没有知识点/问答对（正是事故现场）
TRUNCATED = """## 校对稿
今天我们讲三彩工作流的新版本，这一段很长很长。"""


class TestSplitByChars(unittest.TestCase):
    def test_short_text_is_single_chunk(self):
        self.assertEqual(P.split_by_chars("短文本。", 3500), ["短文本。"])

    def test_empty_text(self):
        self.assertEqual(P.split_by_chars("", 3500), [])

    def test_no_content_is_lost(self):
        """⭐ 拼回去必须和原文一字不差 —— 分块绝不能吞字"""
        text = "第一句。第二句！第三句？第四句；第五句。" * 200
        chunks = P.split_by_chars(text, 500)
        self.assertEqual("".join(chunks), text)

    def test_every_chunk_within_limit(self):
        text = "这是一个比较长的句子，用来测试切分。" * 300
        for c in P.split_by_chars(text, 3500):
            self.assertLessEqual(len(c), 3500)

    def test_does_not_split_mid_sentence(self):
        """切点应落在句末标点后，不该从句子中间切开"""
        text = "甲" * 100 + "。" + "乙" * 100 + "。" + "丙" * 100 + "。"
        chunks = P.split_by_chars(text, 150)
        for c in chunks[:-1]:
            self.assertTrue(c.endswith("。"), f"切点不在句子边界: …{c[-8:]!r}")

    def test_long_single_sentence_is_hard_split(self):
        """单句就超过阈值时，必须硬切而不是无限累积"""
        text = "甲" * 5000
        chunks = P.split_by_chars(text, 1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))


class TestRuleClean(unittest.TestCase):
    def test_removes_leading_filler(self):
        self.assertEqual(P.rule_clean("嗯，今天我们讲工作流。"), "今天我们讲工作流。")

    def test_removes_filler_after_sentence_end(self):
        self.assertIn("第二句", P.rule_clean("第一句。嗯，第二句。"))

    def test_keeps_filler_inside_sentence(self):
        """句子中间的「然后呢」是正常表达，不能删"""
        s = "他先做这个然后呢再做那个"
        self.assertEqual(P.rule_clean(s), s)

    def test_normalizes_whitespace(self):
        self.assertNotIn("   ", P.rule_clean("甲   乙"))

    def test_empty_safe(self):
        self.assertEqual(P.rule_clean(""), "")


class TestMergeCorrected(unittest.TestCase):
    def test_merges_all_three_sections(self):
        m = P.merge_corrected([GOOD, GOOD])
        for s in ("## 校对稿", "## 知识点", "## 问答对"):
            self.assertIn(s, m)

    def test_proofread_is_concatenated_in_order(self):
        a = "## 校对稿\n第一块\n"
        b = "## 校对稿\n第二块\n"
        m = P.merge_corrected([a, b])
        self.assertLess(m.index("第一块"), m.index("第二块"))

    def test_dedupes_identical_knowledge_points(self):
        """多块之间会重复讲同一件事，去重否则库里全是重复条目"""
        m = P.merge_corrected([GOOD, GOOD])
        self.assertEqual(m.count("- 新版三彩推翻上一版的二彩和一彩"), 1)

    def test_dedupes_identical_qa(self):
        m = P.merge_corrected([GOOD, GOOD])
        self.assertEqual(m.count("Q: 新版和旧版差在哪？"), 1)

    def test_keeps_distinct_items(self):
        a = "## 知识点\n- 甲\n- 乙\n"
        b = "## 知识点\n- 丙\n"
        m = P.merge_corrected([a, b])
        for x in ("- 甲", "- 乙", "- 丙"):
            self.assertIn(x, m)


class TestCheckCorrection(unittest.TestCase):
    """⭐ 校验闸门：真检查，不是存在性检查"""

    def test_passes_complete_output(self):
        ok, reasons = P.check_correction([GOOD, GOOD], P.merge_corrected([GOOD, GOOD]))
        self.assertTrue(ok, f"不该报错: {reasons}")
        self.assertEqual(reasons, [])

    def test_fails_when_any_chunk_is_truncated(self):
        """⭐⭐ 事故现场：某块只剩校对稿 → 必须判失败"""
        parts = [GOOD, TRUNCATED]
        ok, reasons = P.check_correction(parts, P.merge_corrected(parts))
        self.assertFalse(ok)
        self.assertTrue(any("截断" in r for r in reasons), reasons)

    def test_fails_when_no_knowledge_section_at_all(self):
        """整体只有校对稿（就是那个 64 分钟视频的情形）"""
        ok, reasons = P.check_correction([TRUNCATED], P.merge_corrected([TRUNCATED]))
        self.assertFalse(ok)
        self.assertTrue(reasons)

    def test_fails_when_knowledge_section_is_title_only(self):
        """真检查：只有 `## 知识点` 标题、没有任何条目，也算不合格"""
        p = "## 知识点\n\n## 问答对\n\n"
        ok, reasons = P.check_correction([p], P.merge_corrected([p]))
        self.assertFalse(ok)

    def test_reports_which_chunk_failed(self):
        parts = [GOOD, TRUNCATED, TRUNCATED]
        ok, reasons = P.check_correction(parts, P.merge_corrected(parts))
        self.assertFalse(ok)
        joined = " ".join(reasons)
        self.assertIn("2", joined)
        self.assertIn("3", joined)


class TestAudioFilter(unittest.TestCase):
    def test_transcribe_uses_denoise_filters(self):
        """降噪（拾语/阿里云那篇都做了）：高通去低频噪、低通去高频噪、响度归一化"""
        args = P.ffmpeg_audio_args("in.mp4", "out.wav")
        joined = " ".join(args)
        self.assertIn("highpass", joined)
        self.assertIn("lowpass", joined)
        self.assertIn("loudnorm", joined)
        self.assertIn("-ar", args)
        self.assertIn("16000", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
