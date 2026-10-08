// 修复主题判断：把知识库里真实的视频标题/关键词写进提示词
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { apiCall } from "./src/core/http.ts";
import { csrfValue } from "./src/core/cookies.ts";
import { refreshConsoleCookies, storeCookies } from "./src/core/auth.ts";

const NODE = process.execPath;
const APP = process.env.DIFY_QA_APP_ID || "<问答应用 app ID>";
const BASE = "http://localhost";
const DS = process.env.DIFY_DATASET_ID || "<你的知识库 ID>";

// ---- 1. 从知识库读出真实内容，生成分类描述 ----
const hf = path.join(os.homedir(), ".difywf", "hosts.json");
const raw = JSON.parse(fs.readFileSync(hf, "utf8"));
const entry = raw.hosts?.[BASE] ?? Object.values(raw.hosts || {})[0];
// ⚠️ 裸调 apiCall 不会自动刷新 token（坑 26）—— 必须先刷，否则 AUTH_EXPIRED
let cookies = entry?.console_cookies ?? {};
const rf = await refreshConsoleCookies(BASE, cookies);
if (rf.ok) { cookies = rf.data; storeCookies(BASE, cookies); console.log("  ✅ token 已刷新"); }
else { console.log("  ⚠️ token 刷新失败，继续用旧的:", JSON.stringify(rf.error).slice(0, 100)); }
const csrf = csrfValue(cookies);

const dl = await apiCall(BASE, `/console/api/datasets/${DS}/documents`, {
  cookies, csrfToken: csrf, query: { page: 1, limit: 50 },
});
const docs = dl.data?.data ?? [];

// 从每个文档的「知识点」段抓关键词
const byTopic = {};
for (const d of docs) {
  const topic = (d.doc_metadata || []).find(m => m.name === "主题")?.value || "其他";
  const sl = await apiCall(BASE, `/console/api/datasets/${DS}/documents/${d.id}/segments`, {
    cookies, csrfToken: csrf, query: { page: 1, limit: 20 },
  });
  const segs = sl.data?.data ?? [];
  const all = segs.map(s => String(s.content || "")).join("\n");
  // 抓出「知识点」列表里的条目，提取关键词
  const pts = [...all.matchAll(/^-\s*(.{4,40}?)[，。；\n]/gm)].map(m => m[1].trim());
  // ⚠️ 一个主题下可能有多个视频，用数组累积，不要用对象覆盖
  if (!byTopic[topic]) byTopic[topic] = [];
  byTopic[topic].push({
    title: d.name.replace(/^(bilibili|douyin|xhs)-/, "").replace(/-\d+$/, "").slice(0, 40),
    points: [...new Set(pts)].slice(0, 8),
  });
}

const docCount = Object.values(byTopic).reduce((a, v) => a + v.length, 0);
console.log(`  从知识库读出 ${docCount} 个文档，分 ${Object.keys(byTopic).length} 类：`);
for (const [t, arr] of Object.entries(byTopic)) {
  console.log(`    【${t}】`);
  for (const v of arr) {
    console.log(`      《${v.title}》`);
    for (const p of v.points.slice(0, 3)) console.log(`          · ${p}`);
  }
}

// ---- 2. 生成新的提示词（**紧凑版**，2026-10-08 改）----
// ⚠️ 旧版把**每个视频的标题 + 8 条知识点**全塞进提示词。库从 6 篇涨到 26 篇后，
//    提示词从 2079 字爆到 6335 字 → **实测导致主题判断退化**：
//    「调色台的操作流程」从 AI视频 被误判成 AI工具 → 该主题文档被元数据过滤挡掉 → 拒答。
//    现在每类只留有限个高辨识度关键词，提示词大小与库规模脱钩。

const KEYWORD_STOP = new Set(["视频", "内容", "方法", "教程", "技巧", "使用", "可以", "需要",
  "进行", "通过", "一个", "这个", "什么", "怎么", "如何", "以下", "包括", "以及"]);

function keywordsOf(arr, cap = 8) {
  const freq = new Map();
  for (const v of arr) {
    for (const s of [v.title, ...v.points]) {
      for (let w of String(s).split(/[，。、；：（）()《》【】\s\/|·\-—:：,]+/)) {
        w = w.trim();
        if (w.length < 2 || w.length > 12) continue;
        if (KEYWORD_STOP.has(w)) continue;
        if (/^[0-9.]+$/.test(w)) continue;
        freq.set(w, (freq.get(w) || 0) + 1);
      }
    }
  }
  return [...freq.entries()].sort((a, b) => b[1] - a[1]).slice(0, cap).map(x => x[0]);
}

const lines = [
  "判断用户的问题跟下面哪一类内容有关，输出类别名。",
  "",
  "知识库分类（括号里是该类的代表性关键词）：",
  "",
];
for (const [topic, arr] of Object.entries(byTopic)) {
  lines.push(`【${topic}】${keywordsOf(arr).join("、")}`);
}
lines.push(
  "",
  "输出规则（必须严格遵守）：",
  // ⚠️ 别硬编码类别数量 —— 加新分类后这里会写错，导致模型输出不存在的类别名
  `只输出上面 ${Object.keys(byTopic).length} 个类别名中的一个，不要标点、不要解释、不要换行、不要空格：`,
  ...Object.keys(byTopic),
  "",
  "判断方法：看用户问的话题最接近哪一类的关键词。",
  "特别注意（容易混的几组）：",
  "· 视频制作的工程词 —— 三彩、一彩、二彩、调色台、显存、MiniMax、H3、生成时长 → AI视频",
  "· 编剧流程词 —— 剧本、分镜、角色设计、配音、对口型、后期剪辑、图生视频、高清放大 → AI编剧",
  "· 短剧制作过程中用到的作图/配音工具词 —— 即梦、可灵、Vidu、海螺、豆包、积分、抽卡、",
  "  反推提示词、绘图提示词、电影写真风格、变清晰、超清、音色克隆 → AI编剧",
  "  （这些是「做短剧时怎么用工具」，不是「Agent/ComfyUI 工具本身」，别归到 AI工具）",
  "· 别因为出现「工作流」「提示词」「节点」「积分」就归到 AI工具 —— 先看它属于哪个业务。",
  "· Agent、ComfyUI、MCP、意图路由、架构选型、RAG → AI工具",
  "· 书籍转技能、SKILL、书籍转成技能 → AI应用（注意：与「短剧编剧课」无关才归这里）",
  "· 电商、详情页、产品图、淘宝、亚马逊 → 电商运营",
  "· 完全看不出属于哪类时，输出：AI工具",
  "",
  "=== 用户的问题 ===",
  "{{#sys.query#}}",
);

const prompt = lines.join("\n");
console.log("\n  新提示词 " + prompt.length + " 字");

const OUT = "_topic-prompt.txt";
fs.writeFileSync(OUT, prompt, "utf8");

// ---- 3. 写回 Dify ----
const r = spawnSync(NODE, ["bin/difywf.js", "workflow", "get-draft",
  "--app-id", APP, "--base-url", BASE],
  { encoding: "utf8", cwd: process.cwd(), maxBuffer: 64 * 1024 * 1024 });
const g = JSON.parse((r.stdout || "").replace(/^\uFEFF/, "")).data.graph;

const node = g.nodes.find(n => n.id === "llm_topic");
if (!node) { console.log("  ❌ 找不到 llm_topic"); process.exit(1); }
node.data.prompt_template[0].text = prompt;
console.log("  ✅ 已写入 llm_topic 节点");

const GF = "_graph-topicfix.json";
fs.writeFileSync(GF, JSON.stringify(g, null, 2), "utf8");

const s = spawnSync(NODE, ["bin/difywf.js", "workflow", "draft", "sync",
  "--app-id", APP, "--graph", GF, "--confirm", "--base-url", BASE],
  { encoding: "utf8", cwd: process.cwd(), maxBuffer: 64 * 1024 * 1024 });
const j = JSON.parse(s.stdout || "{}");
console.log("  同步:", j.ok ? "✅ " + String(j.data.hash).slice(0, 16) : "❌ " + JSON.stringify(j.error).slice(0, 200));
