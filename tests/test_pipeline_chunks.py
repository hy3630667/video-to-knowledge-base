# -*- coding: utf-8 -*-
"""
入库切块测试（one-click-pipeline.build_chunks）

锁住两件事：
  1. **不再有独立的「纯元数据段」**（2026-10-08 改）
     老实现把 【标题】+平台+时长+作者描述 当成 chunks[0]，
     被 SEG_SEP 切成独立分段后，在检索里抢槽位：
     实测「多 Agent 优点」前 10 条里有 3 条是零知识的元数据段（占 30% 槽位）。
     而 【标题】本来就拼在每个内容块开头 → 那一段几乎全是冗余。

  2. 合规策略不回退：只存 知识点 / 问答对 / 关键参数摘录，**不存校对稿**。

跑法：python -m unittest test_pipeline_chunks -v
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 文件名带横线，不能直接 import，用 importlib 载入（和 reingest-compliance.py 一样）
_spec = importlib.util.spec_from_file_location("P", os.path.join(os.path.dirname(HERE), "scripts", "one-click-pipeline.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

INFO = {
    "file": "x.mp4", "title": "测试视频标题", "platform": "douyin",
    "aweme_id": "7656405823026924806", "duration": 233.9,
    "desc": "作者描述内容-SENTINEL-不该出现在库里",
}
PREFIX = f"【{INFO['title']}】\n"

CORRECTED = """## 校对稿
这是他人视频的完整原文-SENTINEL-不应该被写进知识库。
视频总共讲了 7 种架构，耗时 233.9 秒。

## 知识点
- 多Agent架构：优点是任务拆解清晰、降低上下文污染、可扩展性强。

## 问答对
Q: 多Agent架构有什么优点？
A: 任务拆解清晰、降低上下文污染、可扩展性强。
"""


class TestBuildChunks(unittest.TestCase):

    def test_no_standalone_metadata_chunk(self):
        """⭐ 核心回归：不许再有纯元数据段（平台/时长/作者描述 + 无 ## 分节）"""
        chunks = P.build_chunks(INFO, CORRECTED)
        bad = [c for c in chunks
               if ("平台:" in c or "时长:" in c or "作者描述" in c) and "## " not in c]
        self.assertEqual(bad, [], f"不该有独立元数据段，实际出现 {len(bad)} 个: {[b[:50] for b in bad]}")

    def test_author_desc_nowhere_in_output(self):
        """作者描述整段不留（用户 2026-10-08 决定）"""
        joined = "\n".join(P.build_chunks(INFO, CORRECTED))
        self.assertNotIn("SENTINEL", joined)
        self.assertNotIn("作者描述", joined)
        self.assertNotIn("平台:", joined)

    def test_every_chunk_still_has_title_prefix(self):
        """标题前缀必须保留 —— 它是跨视频防串台的关键（不能顺手删）"""
        chunks = P.build_chunks(INFO, CORRECTED)
        self.assertTrue(chunks)
        for c in chunks:
            self.assertTrue(c.startswith(PREFIX), f"缺【标题】前缀: {c[:40]!r}")

    def test_keeps_knowledge_and_qa(self):
        joined = "\n".join(P.build_chunks(INFO, CORRECTED))
        self.assertIn("多Agent架构", joined)
        self.assertIn("问答对", joined)

    def test_drops_proofread_section(self):
        """合规：校对稿（他人完整原文）不许入库"""
        joined = "\n".join(P.build_chunks(INFO, CORRECTED))
        self.assertNotIn("完整原文-SENTINEL", joined)

    def test_rescues_facts_from_proofread(self):
        """从被丢掉的校对稿里只救回含数值的句子"""
        joined = "\n".join(P.build_chunks(INFO, CORRECTED))
        self.assertIn("关键参数", joined)
        self.assertIn("233.9", joined)

    def test_keep_sections_override_still_works(self):
        """回退开关（三节全存）不能被破坏"""
        chunks = P.build_chunks(INFO, CORRECTED, keep_sections=("校对稿", "知识点", "问答对"))
        joined = "\n".join(chunks)
        self.assertIn("完整原文-SENTINEL", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
