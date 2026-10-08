# -*- coding: utf-8 -*-
"""
专名漂移检查的测试

背景（2026-10-08 真实事故）：
  重入库时 LLM「纠错」把转写稿里**正确**的 `Reason加Act` 改成了 `Reason加Actor`，
  直接写进知识库。事后只能靠人工发现。

参考实现：learning-video-skills 列的「三个不会报错但会毁掉产出的坑」之一：
  > **专有名词以 OCR 为准** —— 声学模型解决不了同音词（「短距」/「短剧」），
  > 画面文字才是可靠来源。

理想解是接 OCR 用画面文字做锚。本机没有 OCR，退一步做**可测的替代**：
  1. 从原始转写稿里抽出候选专名（ASCII/技术词）
  2. 纠错后逐个核对：原稿有的专名，纠错稿里还在不在
  3. 不在了 → 用相似度找「疑似的改写结果」，报出来
把「静默改写」变成「可见的告警」。

跑法：python -m unittest test_term_drift -v
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


class TestExtractTerms(unittest.TestCase):
    def test_extracts_ascii_terms(self):
        terms = P.extract_terms("ReAct 架构和 ComfyUI、MiniMax H3 都提到了")
        for t in ("ReAct", "ComfyUI", "MiniMax"):
            self.assertIn(t, terms)

    def test_ignores_short_tokens(self):
        """1-2 个字母的英文多半是误切，不是专名"""
        self.assertNotIn("a", P.extract_terms("a b 和 ComfyUI"))

    def test_stops_words_excluded(self):
        terms = P.extract_terms("The agent uses the tool and the model")
        for w in ("The", "the", "and"):
            self.assertNotIn(w, terms)

    def test_empty_safe(self):
        self.assertEqual(P.extract_terms(""), [])


class TestFindTermDrift(unittest.TestCase):
    def test_detects_act_to_actor(self):
        """⭐ 事故原样：Act 被改成 Actor"""
        drifts = P.find_term_drift("全称是Reason加Act，推理加行动。", "全称是Reason加Actor，推理加行动。")
        self.assertTrue(any(d["term"] == "Act" and d["suspect"] == "Actor" for d in drifts),
                        f"没抓到 Act→Actor: {drifts}")

    def test_suspect_is_the_NEW_word_not_an_existing_one(self):
        """⭐ 真实语料里 ReAct 和 Actor 同时存在，且与 Act 的相似度都是 0.75。
        猜测必须挑「原稿里**没有**的那个词」（Actor），而不是原稿里本来就有的 ReAct。"""
        raw = "ReAct架构：Reason加Act循环。"
        bad = "ReAct架构：Reason加Actor循环。"
        drifts = P.find_term_drift(raw, bad)
        act = [d for d in drifts if d["term"] == "Act"]
        self.assertTrue(act, f"没抓到 Act 漂移: {drifts}")
        self.assertEqual(act[0]["suspect"], "Actor",
                         f"猜错了：应为 Actor（原稿没有的新词），实际 {act[0]['suspect']}")

    def test_no_drift_when_terms_preserved(self):
        self.assertEqual(P.find_term_drift("用 ComfyUI 和 MiniMax", "用 ComfyUI 和 MiniMax"), [])

    def test_term_dropped_without_similar_word(self):
        """专名直接消失（没找到相似词）也要报，只是 suspect 为空"""
        drifts = P.find_term_drift("这里讲 ComfyUI 的用法", "这里讲那个工具的用法")
        self.assertTrue(any(d["term"] == "ComfyUI" for d in drifts))

    def test_ignores_unrelated_new_words(self):
        """纠错稿里出现全新专名（原稿没有），不算漂移"""
        self.assertEqual(P.find_term_drift("讲了 ReAct", "讲了 ReAct 和 Graph"), [])

    def test_empty_inputs_safe(self):
        self.assertEqual(P.find_term_drift("", ""), [])
        self.assertEqual(P.find_term_drift("ComfyUI", ""), [{"term": "ComfyUI", "suspect": None,
                                                            "ratio": 0.0}])


class TestDriftReport(unittest.TestCase):
    def test_formats_report(self):
        drifts = [{"term": "Act", "suspect": "Actor", "ratio": 0.75}]
        s = P.format_drift_report(drifts)
        self.assertIn("Act", s)
        self.assertIn("Actor", s)

    def test_empty_report(self):
        self.assertEqual(P.format_drift_report([]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
