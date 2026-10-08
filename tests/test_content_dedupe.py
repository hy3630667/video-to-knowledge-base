# -*- coding: utf-8 -*-
"""
按内容去重（音频指纹）的测试

背景（2026-10-08 实测）：
  BV1mhJM6XESc 是 19 分P 的短剧制作课，其中 **P2 和 P3 是同一节课的重复投稿** ——
  标题一样（02.脚本分镜技巧）、时长一样（363 秒）、**音频指纹完全相同**（视频重编码过所以文件不同）。
  按分P 无脑入库 → 库里会多一篇几乎一样的文档，检索时互相抢位。

  做法：下载后先算**音频前 N 秒的 md5**，命中已入库的指纹就跳过。
  这样「按内容分」而不是「按链接分」。

跑法：python -m unittest test_content_dedupe -v
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("P", os.path.join(os.path.dirname(HERE), "scripts", "one-click-pipeline.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


class TestFingerprintRegistry(unittest.TestCase):
    """指纹台账的纯逻辑（不碰网络、不碰 ffmpeg）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fpdb_")
        self.db = os.path.join(self.tmp, "fp.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_db_has_no_duplicate(self):
        self.assertIsNone(P.fp_lookup("abc", db_path=self.db))

    def test_record_then_lookup_hits(self):
        P.fp_record("abc123", {"title": "第一课"}, "doc-1", db_path=self.db)
        hit = P.fp_lookup("abc123", db_path=self.db)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["title"], "第一课")
        self.assertEqual(hit["doc_id"], "doc-1")

    def test_different_fingerprint_misses(self):
        P.fp_record("abc123", {"title": "第一课"}, "doc-1", db_path=self.db)
        self.assertIsNone(P.fp_lookup("zzz999", db_path=self.db))

    def test_db_survives_reload(self):
        P.fp_record("abc123", {"title": "第一课"}, "doc-1", db_path=self.db)
        # 模拟进程重启：重新读文件
        again = P.load_fp_db(db_path=self.db)
        self.assertIn("abc123", again)

    def test_corrupt_db_does_not_crash(self):
        with open(self.db, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertIsNone(P.fp_lookup("abc123", db_path=self.db),
                          "台账坏了不该崩，只是查不到")

    def test_none_fingerprint_is_never_a_hit(self):
        """算不出指纹（比如 ffmpeg 失败）时不能误判成重复"""
        P.fp_record("abc123", {"title": "x"}, "d", db_path=self.db)
        self.assertIsNone(P.fp_lookup(None, db_path=self.db))


class TestAudioFingerprint(unittest.TestCase):
    """指纹函数本身（用 ffmpeg 生成素材，验证真的有分辨力）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fpgen_")
        cls.ff = P.FFMPEG
        if not os.path.exists(cls.ff):
            raise unittest.SkipTest("找不到 ffmpeg")
        cls.a = os.path.join(cls.tmp, "sine.wav")
        cls.b = os.path.join(cls.tmp, "silence.wav")
        # 3 秒正弦
        subprocess.run([cls.ff, "-y", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", cls.a], capture_output=True)
        # 3 秒静音
        subprocess.run([cls.ff, "-y", "-v", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=16000:cl=mono", "-t", "3", cls.b], capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_same_file_same_fingerprint(self):
        """同一文件两次必须一样 —— 否则去重会误判"""
        f1 = P.audio_fingerprint(self.a, seconds=3)
        f2 = P.audio_fingerprint(self.a, seconds=3)
        self.assertIsNotNone(f1)
        self.assertEqual(f1, f2)

    def test_different_audio_different_fingerprint(self):
        """⭐ 分辨力：不同内容必须算出不同指纹，否则去重会把不同课当重复丢掉"""
        fa = P.audio_fingerprint(self.a, seconds=3)
        fb = P.audio_fingerprint(self.b, seconds=3)
        self.assertIsNotNone(fa)
        self.assertIsNotNone(fb)
        self.assertNotEqual(fa, fb)

    def test_missing_file_returns_none(self):
        self.assertIsNone(P.audio_fingerprint(os.path.join(self.tmp, "不存在.mp4")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
