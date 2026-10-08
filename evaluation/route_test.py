# -*- coding: utf-8 -*-
"""
主题路由测试 —— 只测「该判到哪个主题」，不测答案

为什么要单独一个：
  主题判断错了，整条检索就废了 —— 元数据过滤会把正确文档挡掉，用户拿到「知识库里没有」。
  2026-10-08 实测踩过：「调色台的操作流程」被判成 AI工具 → MiniMax 文档被过滤 → 拒答。
  而当时的评测集里只有 3 道题能覆盖主题判断 → 调提示词基本靠碰运气。

  这个测试集 30 题覆盖 5 个分类 + 2 道「工作流」陷阱题。跑一次约 2 分钟。

用法：
    python route_test.py                    # 跑全部
    python route_test.py --only T12,T29     # 只跑指定题
    python route_test.py --list             # 只列题
    python route_test.py --baseline         # 存为基线
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SET_FILE = os.path.join(HERE, "路由测试集.json")
BASE_FILE = os.path.join(HERE, "路由基线.json")
OUT_DIR = os.path.join(HERE, "结果")

# ---- 外部依赖路径（可用环境变量覆盖）----
# 开箱默认：node 在 PATH 上；桥接脚本用仓库内的副本。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.environ.get("NODE_BIN", "node")
ASK = os.environ.get("VIDEO_KB_ASK", "<你的 ask.mjs 路径>")   # 见 skill/scripts/ask.mjs
HIT = os.environ.get("VIDEO_KB_PROBE", os.path.join(_REPO, "scripts", "dify-bridges", "_probe-hit.mjs"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def ask_topic(q, timeout=180):
    """问一条，只取 chatflow 自报的『主题』"""
    try:
        r = subprocess.run([NODE, ASK, "--json", q], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"超时 {timeout}s"
    out = (r.stdout or "").strip()
    if not out:
        return None, f"无输出 exit={r.returncode}"
    try:
        return json.loads(out).get("topic", ""), None
    except Exception as e:
        return None, f"JSON 解析失败 {e}"


def main():
    data = json.load(open(SET_FILE, encoding="utf-8"))
    items = data["items"]
    cats = data.get("categories", [])

    if "--list" in sys.argv:
        for it in items:
            print(f"  {it['id']}  {it['expect_topic']:<6}  {it['q']}")
        print(f"\n  共 {len(items)} 题，覆盖 {len(cats)} 个分类")
        return 0

    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        items = [it for it in items if it["id"] in only]

    print("=" * 74)
    print(f"  主题路由测试（{len(items)} 题）")
    print("=" * 74)

    recs, t_all = [], time.time()
    for i, it in enumerate(items, 1):
        got, err = ask_topic(it["q"])
        accept = it.get("accept") or [it["expect_topic"]]
        ok = (got in accept) if got is not None else False
        recs.append({"id": it["id"], "q": it["q"], "expect": accept, "got": got,
                     "ok": ok, "error": err, "note": it.get("note", "")})
        mark = "✅" if ok else "❌"
        extra = f"  ({it['note']})" if it.get("note") and not ok else ""
        print(f"  [{i:>2}/{len(items)}] {mark} {it['id']}  期望 {'/'.join(accept):<6} 实际 {got or '?'}{extra}")
        if err:
            print(f"         ⚠️ {err}")

    passed = [r for r in recs if r["ok"]]
    print("\n" + "=" * 74)
    print(f"  路由准确率: {len(passed)}/{len(recs)}  ({len(passed)/len(recs)*100:.1f}%)")
    print(f"  总耗时    : {time.time()-t_all:.0f} 秒")

    # 按分类的准确率 —— 看是哪一类在拖后腿
    by_cat = {}
    for r in recs:
        k = r["expect"][0]
        by_cat.setdefault(k, [0, 0])
        by_cat[k][1] += 1
        if r["ok"]:
            by_cat[k][0] += 1
    print("  ── 分类明细 ──")
    for k, (a, b) in sorted(by_cat.items()):
        flag = "✅" if a == b else "⚠️"
        print(f"    {flag} {k:<6} {a}/{b}")

    bad = [r for r in recs if not r["ok"]]
    if bad:
        print("  ── 错判明细 ──")
        for r in bad:
            print(f"    ❌ {r['id']} 期望 {'/'.join(r['expect'])} 实际 {r['got'] or '?'}  「{r['q']}」")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out = os.path.join(OUT_DIR, f"route-{stamp}.json")
    summary = {"when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "total": len(recs), "passed": len(passed),
               "by_category": {k: f"{a}/{b}" for k, (a, b) in by_cat.items()},
               "records": recs}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n  明细已存: {out}")

    if "--baseline" in sys.argv:
        with open(BASE_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"  ⭐ 已存为路由基线: {BASE_FILE}")
    elif os.path.isfile(BASE_FILE):
        b = json.load(open(BASE_FILE, encoding="utf-8"))
        print(f"\n  ── 与路由基线对比 ──")
        print(f"  准确率: {b['passed']}/{b['total']}  →  {len(passed)}/{len(recs)}")
        print(f"  基线时间: {b['when']}")

    return 0 if len(passed) == len(recs) else 1


if __name__ == "__main__":
    sys.exit(main())
