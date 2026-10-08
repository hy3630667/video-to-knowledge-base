# -*- coding: utf-8 -*-
"""
评测集**打分器**的双向分辨力自检

为什么需要它（2026-10-08）：
  我们的评测集基线是 **19/19 全过**。全过看着漂亮，但**证明不了打分器能发现问题** ——
  万一打分器写松了，将来真出回归它也会报"通过"。

参考：learning-video-skills 的闸门验证法（README 原文）：
  > 「校验器**双向有分辨力**（未填充模板 **11 错+1 提醒** / 合格笔记 **0 错 0 提醒**）」
  > 「`validate.py` 满分 ≠ 手册可用 —— 它查结构，查不出你有没有读对画面」

→ 所以这里**两头都测**：
   · 喂**坏产出** → 打分器必须判不过（否则它在放水）
   · 喂**好产出** → 打分器必须判过（否则它在误报）

跑法：python -m unittest test_eval_scorer -v
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(os.path.dirname(HERE), "evaluation")
sys.path.insert(0, EVAL_DIR)
_spec = importlib.util.spec_from_file_location("EV", os.path.join(EVAL_DIR, "eval.py"))
EV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EV)


def rec_pass(item, answer, intent="1"):
    """用打分器评一条，返回是否 pass"""
    r = EV.score(item, {"answer": answer, "intent": intent, "topic": "AI工具",
                        "hits": 10, "answerNode": "回复"})
    return r.get("pass"), r


FACT_ITEM = {"id": "T1", "kind": "事实", "q": "x",
             "expect": ["多Agent|Multi-Agent", "Router"], "forbid": ["来源："]}
REFUSE_ITEM = {"id": "T2", "kind": "拒答", "q": "x", "expect": [],
               "expect_refuse": True, "forbid": ["来源："], "expect_intent": "1"}
DIRECT_ITEM = {"id": "T3", "kind": "直答", "q": "x", "expect": [],
               "forbid_refuse": True, "forbid": ["来源："], "expect_intent": "2"}
OUT_ITEM = {"id": "T4", "kind": "越界", "q": "x", "expect": [],
            "expect_decline": True, "forbid": ["来源："], "expect_intent": "2"}


class TestBadOutputIsRejected(unittest.TestCase):
    """⭐ 坏产出必须判不过 —— 这是分辨力的下限"""

    def test_missing_key_points_fails(self):
        ok, r = rec_pass(FACT_ITEM, "这个我不知道。")
        self.assertFalse(ok, f"缺要点却判过了: {r['expect_missing']}")

    def test_partial_key_points_below_threshold_fails(self):
        ok, _ = rec_pass(FACT_ITEM, "只提到了 Router 一个")
        self.assertFalse(ok, "只命中一半要点（50% < 60% 门槛）却判过了")

    def test_forbidden_word_fails(self):
        """「来源：」是用户明确要求不许出现的"""
        ok, r = rec_pass(FACT_ITEM, "多Agent 和 Router。来源：抖音某视频")
        self.assertFalse(ok)
        self.assertIn("来源：", r["forbidden_found"])

    def test_forbidden_term_actor_fails(self):
        """F09 那条负向断言：Actor 是错的（应为 Act）"""
        item = {"id": "T5", "kind": "事实", "q": "x", "expect": ["Reason"],
                "forbid": ["来源：", "Actor"]}
        ok, _ = rec_pass(item, "全称是 Reason 加 Actor，推理加行动。")
        self.assertFalse(ok, "含错误专名 Actor 却判过了")

    def test_refusal_item_answered_normally_fails(self):
        ok, _ = rec_pass(REFUSE_ITEM, "用友 NC 是 ERP 系统，二次开发要先学 UAP。", intent="1")
        self.assertFalse(ok, "该拒答却答了，判过了")

    def test_direct_item_that_refused_fails(self):
        ok, _ = rec_pass(DIRECT_ITEM, "知识库里没有关于这个的内容。", intent="2")
        self.assertFalse(ok, "直答题不该拒答，却判过了")

    def test_out_of_scope_item_without_decline_fails(self):
        ok, _ = rec_pass(OUT_ITEM, "柳州螺蛳粉我推荐这几家……", intent="2")
        self.assertFalse(ok, "越界题没声明帮不上，判过了")

    def test_wrong_intent_fails_for_routed_items(self):
        ok, _ = rec_pass(REFUSE_ITEM, "知识库里没有关于这个的内容。", intent="2")
        self.assertFalse(ok, "意图不符（期望1实际2）却判过了")


class TestGoodOutputIsAccepted(unittest.TestCase):
    """好产出必须判过 —— 否则是误报，会把人逼到不看报告"""

    def test_full_key_points_pass(self):
        ok, r = rec_pass(FACT_ITEM, "包括 多 Agent 架构 和 Router + Skill 架构。")
        self.assertTrue(ok, f"好产出被判不过: {r}")

    def test_english_variant_passes(self):
        """期望值里的 | 写法（多Agent|Multi-Agent）要真的生效"""
        ok, _ = rec_pass(FACT_ITEM, "包括 Multi-Agent 架构 和 Router。")
        self.assertTrue(ok)

    def test_refusal_with_marker_passes(self):
        ok, _ = rec_pass(REFUSE_ITEM, "知识库里没有关于这个的内容。把链接发我，我帮你存。", intent="1")
        self.assertTrue(ok)

    def test_direct_answer_passes(self):
        ok, _ = rec_pass(DIRECT_ITEM, "你好！我是视频知识库问答助手。", intent="2")
        self.assertTrue(ok)

    def test_decline_words_pass(self):
        for w in ("这个问题我答不了", "我查不了天气", "只能帮你查视频里的内容"):
            ok, _ = rec_pass(OUT_ITEM, w, intent="2")
            self.assertTrue(ok, f"「{w}」应算越界声明")


class TestRetrievalScorer(unittest.TestCase):
    """检索层指标的分辨力"""

    ITEM = {"id": "R", "expect_doc": "7种架构"}

    def _probe(self, titles):
        return {"hits": [{"rank": i + 1, "score": 1 - i * 0.1, "is_meta": False,
                          "title": t, "head": t} for i, t in enumerate(titles)]}

    def test_expected_rank_1(self):
        r = EV.score_retrieval(self.ITEM, self._probe(["7种架构 知识点", "别的"]))
        self.assertEqual(r["expect_rank"], 1)
        self.assertTrue(r["p_at_1"])
        self.assertEqual(r["mrr"], 1.0)

    def test_expected_rank_4_gives_mrr_quarter(self):
        r = EV.score_retrieval(self.ITEM, self._probe(["a", "b", "c", "7种架构 知识点"]))
        self.assertEqual(r["expect_rank"], 4)
        self.assertFalse(r["p_at_1"])
        self.assertAlmostEqual(r["mrr"], 0.25)

    def test_expected_missing(self):
        r = EV.score_retrieval(self.ITEM, self._probe(["a", "b"]))
        self.assertIsNone(r["expect_rank"])
        self.assertEqual(r["mrr"], 0.0)
        self.assertEqual(r["noise_ratio"], 1.0)

    def test_counts_metadata_segments(self):
        p = self._probe(["7种架构 知识点", "x"])
        p["hits"][1]["is_meta"] = True
        r = EV.score_retrieval(self.ITEM, p)
        self.assertEqual(r["meta_segments"], 1)

    def test_matches_nested_bracket_title_via_head(self):
        """⭐ 实测踩过：标题是「【【321期】…】」，从【】里抠只会得到「【321期」，
        所以必须 title + head 一起匹配"""
        item = {"id": "R2", "expect_doc": "书籍转技能"}
        probe = {"hits": [{"rank": 1, "score": 0.7, "is_meta": False,
                           "title": "【321期", "head": "【【321期】书籍转技能怎么用？】 ## 知识点"}]}
        r = EV.score_retrieval(item, probe)
        self.assertEqual(r["expect_rank"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
