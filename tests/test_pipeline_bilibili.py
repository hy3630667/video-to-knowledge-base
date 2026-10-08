# -*- coding: utf-8 -*-
"""
B站分P 下载的测试

背景（2026-10-08 实测事故风险）：
  `download_bilibili()` 取 `fetch_one_video` 返回的**顶层 cid（= P1）**，
  然后只下这一路 dash 流 → **多分P 课程只会入库 P1，其余静默丢失、不报错**。
  实测样本 BV1mhJM6XESc：19 分P / 总 124.5 分钟 → 按旧逻辑丢 113 分钟。

参考实现：拾语 shiyu-ai-knowledge-base——「多分P系列一键排队批量总结并合并合集笔记」。

跑法：python -m unittest test_pipeline_bilibili -v
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

VIDEO = "01.剧本撰写方法"
PAGES19 = [
    {"page": 1, "cid": 111, "part": "01.剧本撰写方法", "duration": 664},
    {"page": 2, "cid": 222, "part": "02.脚本分镜技巧", "duration": 363},
    {"page": 19, "cid": 999, "part": "18.补充课程-多角色对口型", "duration": 67},
]


class TestPartTitles(unittest.TestCase):
    def test_single_part_keeps_plain_title(self):
        """单P 视频标题不该被加 P1 前缀（否则库名全变）"""
        got = P.build_part_titles("普通单P视频", [{"page": 1, "cid": 1, "part": "正片"}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "普通单P视频")

    def test_multi_part_titles_include_page_and_name(self):
        got = P.build_part_titles(VIDEO, PAGES19)
        self.assertEqual(len(got), 3)
        self.assertIn("P1", got[0]["title"])
        self.assertIn("剧本撰写方法", got[0]["title"])
        self.assertIn("P19", got[2]["title"])

    def test_titles_are_unique(self):
        """⭐ 文档名由标题生成；标题重了 → 库里两篇同名文档互相覆盖/混淆"""
        got = P.build_part_titles(VIDEO, PAGES19)
        titles = [g["title"] for g in got]
        self.assertEqual(len(titles), len(set(titles)))

    def test_no_pages_falls_back_to_single(self):
        """有些视频没有 pages 字段 —— 不能崩，退回单P"""
        got = P.build_part_titles(VIDEO, [])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], VIDEO)

    def test_cid_is_carried_through(self):
        got = P.build_part_titles(VIDEO, PAGES19)
        self.assertEqual([g["cid"] for g in got], [111, 222, 999])

    def test_title_is_length_capped(self):
        got = P.build_part_titles("很长的标题" * 30, [{"page": 1, "cid": 1, "part": "x"}])
        self.assertLessEqual(len(got[0]["title"]), 60)


class TestSplitFilenames(unittest.TestCase):
    def test_multi_part_filename_includes_page(self):
        """两个分P 不能写同一个文件（否则后一个覆盖前一个）"""
        f1 = P.part_filename("BV1xx", 1, "01.剧本撰写方法")
        f2 = P.part_filename("BV1xx", 2, "02.脚本分镜技巧")
        self.assertNotEqual(f1, f2)
        self.assertIn("P1", f1)
        self.assertIn("P2", f2)

    def test_filename_is_windows_safe(self):
        f = P.part_filename("BV1xx", 3, 'a/b:c*d?e"f<g>h|i')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, os.path.basename(f))


if __name__ == "__main__":
    unittest.main(verbosity=2)
