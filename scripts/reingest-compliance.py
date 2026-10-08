# -*- coding: utf-8 -*-
"""
用本地已下载的视频重跑入库（合规版：只存知识点 + 问答对）

背景：
  原来知识库里存了「校对稿」—— 那是他人视频的完整原文，
  存进公司知识库法律风险最高（著作权法的思想/表达二分法）。
  现在 build_chunks 默认丢掉「校对稿」，但已有的 4 个文档还是老内容，
  需要用本地视频重跑一遍。

为什么用本地视频：
  视频文件都还在本地工作目录，不用重新下载（省时，也避开链接失效）。

用法：
    python reingest-compliance.py            # 先干跑，列出将处理什么
    python reingest-compliance.py --go       # 真正执行
    python reingest-compliance.py --go --only MiniMax
"""
import io
import os
import re
import sys
import time
import importlib.util

# ⚠️ 不要在这里包装 sys.stdout —— one-click-pipeline.py 在 import 时会自己包一层，
#    先包的话它会把我们的包装顶掉并关闭底层 buffer（ValueError: I/O operation on closed file）。
#    模块 import 完之后 sys.stdout 已经是 UTF-8 的了，直接 print 即可。

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("P", os.path.join(HERE, "one-click-pipeline.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

GO = "--go" in sys.argv
ONLY = None
if "--only" in sys.argv:
    ONLY = sys.argv[sys.argv.index("--only") + 1]

WORK = P.WORK_DIR   # 从 config.py 或环境变量读

# 从文件名反解 info（文件名格式：平台_awemeid_标题.mp4）
def parse_name(fn):
    base = os.path.splitext(os.path.basename(fn))[0]
    parts = base.split("_", 2)
    platform = parts[0] if len(parts) > 0 else "unknown"
    aweme_id = parts[1] if len(parts) > 1 else ""
    title = parts[2] if len(parts) > 2 else base
    title = title.replace("_", " ").strip()
    return platform, aweme_id, title


def main():
    if not os.path.isdir(WORK):
        print(f"❌ 工作目录不存在: {WORK}")
        return 1

    files = sorted(
        [f for f in os.listdir(WORK) if f.lower().endswith((".mp4", ".mkv", ".webm", ".m4a", ".mp3"))],
        key=lambda x: os.path.getsize(os.path.join(WORK, x)),
    )
    files = [f for f in files if not f.startswith("douyin-test") and not f.startswith("xhs_")]

    print("=" * 74)
    print("  合规重入库：只存「知识点 + 问答对」")
    print("=" * 74)
    print(f"  工作目录   : {WORK}")
    print(f"  保留的分节 : {P.DEFAULT_KEEP_SECTIONS}")
    print(f"  知识库     : {P.DEFAULT_DATASET}")
    print(f"  模式       : {'🔴 真正执行' if GO else '🟡 干跑（不改任何东西）'}")
    print()
    print(f"  待处理 {len(files)} 个视频：")
    for f in files:
        plat, aid, title = parse_name(f)
        sz = os.path.getsize(os.path.join(WORK, f)) / 1024 / 1024
        print(f"    [{plat:9}] {sz:6.1f} MB  {title[:48]}")
    print()

    if ONLY:
        files = [f for f in files if ONLY in f]
        print(f"  按 --only 过滤后剩 {len(files)} 个")
        print()

    if not GO:
        print("  ⚠️ 这是干跑。确认无误后加 --go 真正执行。")
        print("     执行会：重新转写 → LLM 纠错 → 覆盖知识库里的同名文档")
        return 0

    ok, fail = 0, 0
    for idx, fn in enumerate(files, 1):
        path = os.path.join(WORK, fn)
        plat, aid, title = parse_name(fn)
        print("─" * 74)
        print(f"  [{idx}/{len(files)}] {title[:56]}")
        print(f"        平台={plat}  id={aid}  文件={fn}")

        info = {
            "file": path, "title": title, "platform": plat, "aweme_id": aid,
            "duration": 0.0, "desc": "",
        }
        try:
            info["duration"] = P.probe_duration(path) or 0.0
        except Exception:
            pass

        t0 = time.time()
        try:
            print(f"        ① 转写中（时长 {info['duration']:.0f} 秒）...")
            tr = P.transcribe(path, "small")
            if not tr:
                print("        ❌ 转写失败")
                fail += 1
                continue
            print(f"           {len(tr['text'])} 字，{tr['ratio']:.2f}x")

            print("        ② LLM 纠错 + 知识点提取...")
            corrected = P.correct_transcript(info, tr)
            if not corrected:
                print("        ⚠️ 纠错失败，跳过（不写低质量内容）")
                fail += 1
                continue
            n_sec = len(re.findall(r"(?m)^##\s", corrected))
            print(f"           纠错稿 {len(corrected)} 字，{n_sec} 个分节")

            print("        ③ 切块（丢掉校对稿）...")
            chunks = P.build_chunks(info, corrected)
            total = sum(len(c) for c in chunks)
            has_tr = any("校对稿" in c[:60] for c in chunks)
            print(f"           {len(chunks)} 块 / {total} 字，含校对稿: {'❌ 是' if has_tr else '✅ 否'}")

            print("        ④ 写入知识库（覆盖同名文档）...")
            res = P.write_to_dify(info, tr, P.DEFAULT_DATASET, corrected)
            if not res:
                print("        ❌ 写入失败")
                fail += 1
                continue
            print(f"           文档 id: {res['doc_id']}")
            P.verify(P.DEFAULT_DATASET, title)
            print(f"        ✅ 完成，耗时 {time.time()-t0:.1f} 秒")
            ok += 1

        except Exception as e:
            print(f"        ❌ 异常: {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    print("=" * 74)
    print(f"  完成：成功 {ok} / 失败 {fail} / 共 {len(files)}")
    print("=" * 74)
    print()
    print("  ⚠️ 别忘了重新打主题元数据（新建的文档没有「主题」标签）：")
    print("     node _bind-topics.mjs   （在 dify-mcp 目录下）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
