# -*- coding: utf-8 -*-
"""
评测集运行器 —— 视频知识库问答的回归测试

干什么：
  把 评测集.json 里的题逐条问一遍 chatflow，按「要点命中 / 拒答是否正确 / 意图是否正确 /
  禁用词是否出现」打分，输出报告并存档。

为什么要它：
  改 top_k、改阈值、改提示词、重入库之后，**没有它就无法证明是变好还是变坏**。
  在此之前所有检索调优都是盲调。

用法：
    python eval.py                      # 跑全部
    python eval.py --only F01,X01       # 只跑指定题
    python eval.py --baseline           # 跑完并存为基线.json
    python eval.py --list               # 只列题，不跑

⚠️ 这些题会真的写进 Dify 的 messages 表 → 会进监控。
   monitor/common.py 已把本评测集里的题**当测试流量排除**，不污染真实指标。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# ⚠️ 中文控制台默认可能是 GBK，输出 ✅/❌ 会直接抛 UnicodeEncodeError 把整个评测打断。
#    这里强制 UTF-8（写到管道/文件时同样生效）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SET_FILE = os.path.join(HERE, "评测集.json")
OUT_DIR = os.path.join(HERE, "结果")
BASE_FILE = os.path.join(HERE, "基线.json")

# ---- 外部依赖路径（可用环境变量覆盖）----
# 开箱默认：node 在 PATH 上；桥接脚本用仓库内的副本。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.environ.get("NODE_BIN", "node")
ASK = os.environ.get("VIDEO_KB_ASK", "<你的 ask.mjs 路径>")   # 见 skill/scripts/ask.mjs
HIT = os.environ.get("VIDEO_KB_PROBE", os.path.join(_REPO, "scripts", "dify-bridges", "_probe-hit.mjs"))

REFUSE_MARK = "知识库里没有"
INTENT_NAME = {"1": "查库", "2": "直答", "3": "入库"}

# 「越界拒答」的措辞标志：问的是库外的事，助手说明自己帮不上。
# ⚠️ 这类问题会被**意图判断路由到「直答」分支**，所以不会出现「知识库里没有」那句话
#    （那句只属于「查库」分支的拒答路径）。期望值必须按分支分开写，否则误判。
DECLINE_MARKS = ["答不了", "查不了", "只能帮你查", "帮不上", "无法回答", "没法", "知识库里没有"]

# 命中率门槛：低于它就判这题没过
PASS_HIT_RATE = 0.6


def _norm(s):
    """匹配前归一化：去掉空白/标点/加粗符号。

    为什么必须做：实测答案是「单 Agent 架构」（带空格）、「不太稳定」，
    而期望值若写成「单Agent」「不稳定」就会假失败 —— 那是评测器的错，不是系统的错。
    """
    if not s:
        return ""
    s = str(s).lower()
    for ch in (" ", "\u3000", "\t", "\n", "\r", "*", "「", "」", "“", "”",
               "，", ",", "。", ".", "、", "；", ";", "：", ":", "（", "）", "(", ")", "—", "-"):
        s = s.replace(ch, "")
    return s


def _hit(entry, norm_answer):
    """一个期望条目命中与否。条目里用 | 表示「或」（例如 不稳定|不太稳定|不够稳定）"""
    return any(_norm(alt) and _norm(alt) in norm_answer for alt in str(entry).split("|"))


def load_set():
    with open(SET_FILE, encoding="utf-8") as f:
        return json.load(f)


def ask_one(q, timeout=240):
    """问一条，返回 {intent, topic, hits, answer, answerNode, error}"""
    try:
        r = subprocess.run([NODE, ASK, "--json", q],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"超时 {timeout}s"}
    out = (r.stdout or "").strip()
    if not out:
        return {"error": f"没有输出（exit={r.returncode}）: {(r.stderr or '')[:120]}"}
    try:
        return json.loads(out)
    except Exception as e:
        return {"error": f"JSON 解析失败: {e} :: {out[:120]}"}


def probe_retrieval(q, topic, top_k=10):
    """跑检索测试，拿检索层指标。

    ⭐ 为什么要有这一层：只看「答案对不对」测不出 top_k 的取舍 ——
       答案对了不代表检索对了（模型可能靠别的片段蒙对）。
       检索层（期望视频排第几 / 噪音占比 / 元数据段条数）才是敏感指标。

    topic 用 chatflow 自己判出来的主题，保证和真实链路一致。
    """
    if not topic:
        return None
    try:
        r = subprocess.run([NODE, HIT, "--json", "--topic", topic, "--top-k", str(top_k), q],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        arr = json.loads((r.stdout or "").strip())
        return arr[0] if arr else None
    except Exception as e:
        return {"error": f"{e}"}


def score(item, res):
    """按题型打分"""
    ans = res.get("answer") or ""
    low = ans.lower()
    kind = item["kind"]

    rec = {
        "id": item["id"], "kind": kind, "q": item["q"], "answer": ans,
        "intent": res.get("intent", ""), "topic": res.get("topic", ""),
        "hits": res.get("hits", 0), "answerNode": res.get("answerNode", ""),
        "error": res.get("error", ""),
    }

    # 禁用词（例如「来源：」）
    bad = [w for w in item.get("forbid", []) if w in ans]
    rec["forbidden_found"] = bad
    rec["forbid_ok"] = not bad

    # 要点命中
    exp = item.get("expect") or []
    na = _norm(ans)
    found = [e for e in exp if _hit(e, na)]
    missing = [e for e in exp if not _hit(e, na)]
    rec["expect_found"] = found
    rec["expect_missing"] = missing
    rec["hit_rate"] = (len(found) / len(exp)) if exp else None

    # 意图
    ei = item.get("expect_intent")
    rec["intent_ok"] = (res.get("intent", "") == ei) if ei else None

    # 拒答（两种分开判，因为走的分支不同）
    refused = REFUSE_MARK in ans                       # 查库分支的拒答
    declined = any(m in ans for m in DECLINE_MARKS)     # 越界说明（含直答分支）
    rec["refused"] = refused
    rec["declined"] = declined
    if item.get("expect_refuse"):
        # 必须走查库分支并说明库里没有
        rec["pass"] = refused and rec["forbid_ok"] and (rec["intent_ok"] is not False)
    elif item.get("expect_decline"):
        # 越界：只要明确说自己帮不上、且没编造就行（不要求「知识库里没有」那句话）
        rec["pass"] = declined and rec["forbid_ok"] and (rec["intent_ok"] is not False)
    elif item.get("forbid_refuse"):
        rec["pass"] = (not refused) and rec["forbid_ok"] and (rec["intent_ok"] is not False)
    elif kind.endswith("仅记录"):
        rec["pass"] = None
    elif exp:
        rec["pass"] = (rec["hit_rate"] >= PASS_HIT_RATE) and rec["forbid_ok"]
    else:
        rec["pass"] = None

    if rec.get("error"):
        rec["pass"] = False
    return rec


def score_retrieval(item, probe):
    """检索层指标：期望视频排第几、噪音多少、有没有元数据段"""
    if not probe or probe.get("error"):
        return {"error": (probe or {}).get("error", "无检索数据")}
    exp = item.get("expect_doc")

    def is_expected(h):
        # ⚠️ 不能只匹配从【】里抠出来的 title —— 有的标题本身带方括号
        #    （例：「【【321期】书籍转技能…】」会抠成「【321期」），
        #    所以 title 和 head 一起匹配。
        if not exp:
            return False
        return exp in (h.get("title") or "") or exp in (h.get("head") or "")

    hits = probe.get("hits") or []
    ranks = [h["rank"] for h in hits if is_expected(h)]
    first = min(ranks) if ranks else None
    noise = [h for h in hits if not is_expected(h)]
    return {
        "hit_count": len(hits),
        "expect_rank": first,                             # 期望视频首次出现的排名
        "p_at_1": first == 1,
        "mrr": (1.0 / first) if first else 0.0,
        "noise_ratio": (len(noise) / len(hits)) if hits else None,
        "meta_segments": sum(1 for h in hits if h.get("is_meta")),
        "top_score": hits[0]["score"] if hits else None,
        "titles": [h.get("title", "")[:22] for h in hits],
    }


def main():
    items = load_set()["items"]
    if "--list" in sys.argv:
        for it in items:
            print(f"  {it['id']:5} [{it['kind']:9}] {it['q']}")
        print(f"\n  共 {len(items)} 题")
        return 0

    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        items = [it for it in items if it["id"] in only]

    print("=" * 78)
    print(f"  视频知识库 · 评测集  （{len(items)} 题）")
    print("=" * 78)

    recs = []
    t_all = time.time()
    for i, it in enumerate(items, 1):
        print(f"\n[{i}/{len(items)}] {it['id']} [{it['kind']}] {it['q']}")
        t0 = time.time()
        res = ask_one(it["q"])
        rec = score(it, res)
        # 检索层指标（只有带期望视频的题才算）
        if it.get("expect_doc") and not rec.get("error"):
            rec["retrieval"] = score_retrieval(it, probe_retrieval(it["q"], rec.get("topic")))
        rec["elapsed"] = round(time.time() - t0, 2)
        recs.append(rec)
        if rec.get("error"):
            print(f"      ❌ {rec['error']}")
            continue
        mark = {True: "✅", False: "❌", None: "·"}.get(rec.get("pass"), "·")
        print(f"      {mark} 意图={INTENT_NAME.get(rec['intent'], rec['intent'] or '?')}"
              f"  命中={rec['hit_rate'] if rec['hit_rate'] is None else format(rec['hit_rate'], '.0%')}"
              f"  检索={rec['hits']}条  {rec['elapsed']}s")
        r = rec.get("retrieval")
        if r and not r.get("error"):
            flag = "✅" if r["p_at_1"] else ("⚠️" if r["expect_rank"] else "❌")
            print(f"         {flag} 期望视频排第 {r['expect_rank'] or '未命中'}"
                  f"  P@1={'是' if r['p_at_1'] else '否'}  MRR={r['mrr']:.2f}"
                  f"  噪音={r['noise_ratio']:.0%}  元数据段={r['meta_segments']}")
        elif r and r.get("error"):
            print(f"         ⚠️ 检索层取不到: {r['error'][:60]}")
        if rec["expect_missing"]:
            print(f"         缺: {', '.join(rec['expect_missing'])}")
        if rec["forbidden_found"]:
            print(f"         ⚠️ 出现禁用词: {rec['forbidden_found']}")

    # ---- 汇总 ----
    scored = [r for r in recs if r.get("pass") is not None]
    passed = [r for r in scored if r["pass"]]
    rates = [r["hit_rate"] for r in recs if r["hit_rate"] is not None]
    forb = [r for r in recs if not r["forbid_ok"]]
    intent_bad = [r for r in recs if r["intent_ok"] is False]

    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    print(f"  计分题通过    : {len(passed)}/{len(scored)}"
          f"  ({len(passed)/len(scored)*100:.1f}%)" if scored else "  计分题通过    : 0/0")
    if rates:
        avg = sum(rates) / len(rates)
        print(f"  平均要点命中  : {avg:.1%}   （{sum(1 for r in rates if r >= PASS_HIT_RATE)}/{len(rates)} 题达标）")
    print(f"  意图不符      : {len(intent_bad)} 条"
          + (f"  → {', '.join(r['id'] for r in intent_bad)}" if intent_bad else "  ✅"))
    print(f"  出现禁用词    : {len(forb)} 条"
          + (f"  → {', '.join(r['id'] for r in forb)}" if forb else "  ✅（「来源」已彻底去掉）"))

    # ---- 检索层汇总 ----
    rets = [r["retrieval"] for r in recs if isinstance(r.get("retrieval"), dict) and not r["retrieval"].get("error")]
    if rets:
        p1 = sum(1 for x in rets if x["p_at_1"])
        mrr = sum(x["mrr"] for x in rets) / len(rets)
        nois = [x["noise_ratio"] for x in rets if x["noise_ratio"] is not None]
        meta = sum(x["meta_segments"] for x in rets)
        miss = [r["id"] for r in recs
                if isinstance(r.get("retrieval"), dict) and not r["retrieval"].get("error")
                and not r["retrieval"]["expect_rank"]]
        print(f"  ── 检索层（{len(rets)} 题有期望视频）──")
        print(f"  P@1（期望视频排第1）: {p1}/{len(rets)}  ({p1/len(rets):.0%})")
        print(f"  MRR               : {mrr:.3f}")
        if nois:
            print(f"  平均噪音占比      : {sum(nois)/len(nois):.0%}  （非期望视频的片段）")
        print(f"  元数据段总数      : {meta}" + ("  ✅ 已清零" if meta == 0 else "  ⚠️ 仍在抢槽位"))
        if miss:
            print(f"  期望视频未命中    : {', '.join(miss)}")
    print(f"  总耗时        : {time.time()-t_all:.0f} 秒")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out = os.path.join(OUT_DIR, f"run-{stamp}.json")
    summary = {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(scored), "passed": len(passed),
        "avg_hit_rate": (sum(rates) / len(rates)) if rates else None,
        "intent_mismatch": [r["id"] for r in intent_bad],
        "forbidden": [r["id"] for r in forb],
        "retrieval": {
            "n": len(rets),
            "p_at_1": sum(1 for x in rets if x["p_at_1"]),
            "mrr": (sum(x["mrr"] for x in rets) / len(rets)) if rets else None,
            "avg_noise": (sum(x["noise_ratio"] for x in rets if x["noise_ratio"] is not None)
                          / max(1, len([x for x in rets if x["noise_ratio"] is not None]))) if rets else None,
            "meta_segments": sum(x["meta_segments"] for x in rets) if rets else None,
        },
        "records": recs,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  明细已存      : {out}")

    if "--baseline" in sys.argv:
        with open(BASE_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"  ⭐ 已存为基线  : {BASE_FILE}")

    # 与基线对比
    if os.path.isfile(BASE_FILE) and "--baseline" not in sys.argv:
        try:
            base = json.load(open(BASE_FILE, encoding="utf-8"))
            print("\n  ── 与基线对比 ──")
            print(f"  通过  : {base['passed']}/{base['total']}  →  {len(passed)}/{len(scored)}")
            if base.get("avg_hit_rate") and rates:
                print(f"  命中  : {base['avg_hit_rate']:.1%}  →  {sum(rates)/len(rates):.1%}")
            b_ret = base.get("retrieval") or {}
            n_ret = summary.get("retrieval") or {}
            if b_ret.get("n") and n_ret.get("n"):
                print(f"  P@1   : {b_ret['p_at_1']}/{b_ret['n']}  →  {n_ret['p_at_1']}/{n_ret['n']}")
                if b_ret.get("mrr") is not None and n_ret.get("mrr") is not None:
                    print(f"  MRR   : {b_ret['mrr']:.3f}  →  {n_ret['mrr']:.3f}")
                if b_ret.get("avg_noise") is not None and n_ret.get("avg_noise") is not None:
                    print(f"  噪音  : {b_ret['avg_noise']:.0%}  →  {n_ret['avg_noise']:.0%}")
                if b_ret.get("meta_segments") is not None and n_ret.get("meta_segments") is not None:
                    print(f"  元数据段: {b_ret['meta_segments']}  →  {n_ret['meta_segments']}")
            print(f"  基线时间: {base['when']}")
        except Exception as e:
            print(f"  （基线读取失败: {e}）")

    return 0 if len(passed) == len(scored) else 1


if __name__ == "__main__":
    sys.exit(main())
