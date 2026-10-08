// 跑「检索测试」，看某个问题实际捞到哪些分段、分数多少。
//
// 用途：
//   ① 调 top_k / 阈值前先看检索这一层，别猜
//   ② 评测集拿它算**检索层指标**（P@1 / MRR / 噪音占比 / 元数据段占比）——
//      光看「答案对不对」测不出 top_k 的取舍，检索层才敏感
//
// 用法：
//   node _probe-hit.mjs "问题1" "问题2"                 # 人看（默认主题=AI工具）
//   node _probe-hit.mjs --topic AI视频 "问题"           # 指定主题过滤（用 chatflow 判出的主题）
//   node _probe-hit.mjs --nofilter "问题"               # 不过滤主题，看自然排名
//   node _probe-hit.mjs --json --topic AI工具 "问题"    # 输出 JSON（评测集用）
//   node _probe-hit.mjs --top-k 5 "问题"
import fs from "node:fs"; import path from "node:path"; import os from "node:os";
import { apiCall } from "./src/core/http.ts";
import { csrfValue } from "./src/core/cookies.ts";
import { refreshConsoleCookies, storeCookies } from "./src/core/auth.ts";

const BASE = "http://localhost";
const DS = process.env.DIFY_DATASET_ID || "<你的知识库 ID>";
const META = process.env.DIFY_TOPIC_METADATA_ID || "<主题元数据字段 ID>";

const argv = process.argv.slice(2);
const flag = (name, def = null) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : def;
};
const NOFILTER = argv.includes("--nofilter");
const AS_JSON = argv.includes("--json");
const TOP_K = Number(flag("--top-k", 10));
const TOPIC = flag("--topic", "AI工具");
const WITH_VALUE = ["--topic", "--top-k"];
const queries = argv.filter((a, i) => !a.startsWith("--") && !WITH_VALUE.includes(argv[i - 1]));
if (!queries.length) { console.log('用法: node _probe-hit.mjs [--topic 主题] [--json] [--top-k N] "问题"'); process.exit(1); }

const hf = path.join(os.homedir(), ".difywf", "hosts.json");
let cookies = (JSON.parse(fs.readFileSync(hf, "utf8")).hosts?.[BASE] ?? {})?.console_cookies ?? {};
const rf = await refreshConsoleCookies(BASE, cookies);
if (rf.ok) { cookies = rf.data; storeCookies(BASE, cookies); }
const csrf = csrfValue(cookies);

const isMetaSegment = (c) => c.includes("平台:") && !c.includes("## 知识点") && !c.includes("## 问答对") && !c.includes("## 关键参数");

const out = [];
for (const q of queries) {
  const retrieval_model = {
    search_method: "hybrid_search", reranking_enable: true, reranking_mode: "weighted_score",
    weights: {
      vector_setting: { vector_weight: 0.5, embedding_provider_name: "langgenius/ollama/ollama", embedding_model_name: "nomic-embed-text" },
      keyword_setting: { keyword_weight: 0.5 },
    },
    top_k: TOP_K, score_threshold_enabled: false,
  };
  if (!NOFILTER) {
    retrieval_model.metadata_filtering_mode = "manual";
    retrieval_model.metadata_filtering_conditions = {
      logical_operator: "and",
      conditions: [{ id: "c1", metadata_id: META, name: "主题", comparison_operator: "is", value: TOPIC }],
    };
  }
  let recs = [];
  let err = null;
  try {
    const r = await apiCall(BASE, `/console/api/datasets/${DS}/hit-testing`, { method: "POST", cookies, csrfToken: csrf, body: { query: q, retrieval_model } });
    recs = r.data?.records || [];
  } catch (e) { err = e.message; }

  const hits = recs.map((x, i) => {
    const c = String(x.segment?.content || "");
    return {
      rank: i + 1,
      score: Number(x.score || 0),
      chars: c.length,
      is_meta: isMetaSegment(c),
      // 每个片段都以【视频标题】开头（标题前缀，防串台用）→ 用它认来源，不依赖文档名
      title: (c.match(/^【(.+?)】/) || [, ""])[1],
      head: c.replace(/\n/g, " ").slice(0, 90),
    };
  });
  out.push({ query: q, topic: NOFILTER ? null : TOPIC, top_k: TOP_K, error: err, hit_count: hits.length, hits });

  if (!AS_JSON) {
    console.log(`\n### ${q}`);
    console.log(`    主题过滤: ${NOFILTER ? "关" : TOPIC}   top_k=${TOP_K}   命中 ${hits.length} 条${err ? "  ❌ " + err : ""}`);
    for (const h of hits) {
      console.log(`    ${String(h.rank).padStart(2)}. ${h.score.toFixed(4)}  [${h.chars}字${h.is_meta ? " ⚠️元数据段" : ""}] ${h.head.slice(0, 70)}`);
    }
  }
}

if (AS_JSON) console.log(JSON.stringify(out, null, 2));
