// 向视频知识库提问（skill 专用）
//
// 用法：
//   node ask.mjs "三彩工作流是什么？"
//   node ask.mjs --json "三彩工作流是什么？"
//
// 为什么单独写一个：chatflow 一次会推 600+ 个 SSE 事件，
// 在 PowerShell 里 ConvertFrom-Json 容易丢字段。这个脚本只取需要的那几个节点。

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const APP = "<问答应用 app ID>";
const BASE = "http://localhost";
const DIFY_MCP = "<你的 dify-mcp 目录>";
const NODE = process.execPath;

const args = process.argv.slice(2);
const asJson = args.includes("--json");
const q = args.filter(a => a !== "--json").join(" ").trim();

if (!q) {
  console.error('用法: node ask.mjs "你的问题"');
  process.exit(1);
}

const r = spawnSync(NODE, [
  path.join(DIFY_MCP, "bin", "difywf.js"),
  "workflow", "run-draft",
  "--app-id", APP,
  "--inputs", JSON.stringify({ query: q }),
  "--query", q,
  "--base-url", BASE,
], { encoding: "utf8", cwd: DIFY_MCP, maxBuffer: 64 * 1024 * 1024 });

let j;
try {
  j = JSON.parse((r.stdout || "").replace(/^\uFEFF/, ""));
} catch (e) {
  console.error("❌ CLI 输出解析失败");
  console.error((r.stdout || r.stderr || "").slice(0, 400));
  process.exit(1);
}

if (!j.ok) {
  console.error("❌ 运行失败:", JSON.stringify(j.error).slice(0, 300));
  process.exit(1);
}

const evs = Array.isArray(j.data) ? j.data : Object.values(j.data || {});

const o = { intent: "", topic: "", hits: 0, answer: "", answerNode: "" };

for (const e of evs) {
  if (e.event !== "node_finished") continue;
  const t = e.data?.title || "";
  const out = e.data?.outputs || {};

  if (t.includes("意图判断")) o.intent = String(out.text ?? "").trim();
  if (t.includes("主题判断")) o.topic = String(out.text ?? "").trim();
  if (t.includes("知识检索（改写后）")) {
    o.hits = Array.isArray(out.result) ? out.result.length : 0;
  }
  // 答案是 answer 节点的 answer 字段（最终出口，已合并两轮重试）
  if (t.startsWith("回复")) {
    const a = String(out.answer ?? "").trim();
    if (a) { o.answer = a; o.answerNode = t; }
  }
  // 兜底：拿 LLM 的 text
  if (!o.answer && (t.includes("基于片段回答") || t.includes("直接回答"))) {
    const a = String(out.text ?? "").trim();
    if (a) { o.answer = a; o.answerNode = t; }
  }
}

// 如果 answer 节点是空的，回退到最后一个有内容的 LLM 输出
if (!o.answer) {
  for (const e of evs) {
    if (e.event !== "node_finished") continue;
    const t = e.data?.title || "";
    if (!/基于片段回答|直接回答/.test(t)) continue;
    const a = String(e.data?.outputs?.text ?? "").trim();
    if (a) { o.answer = a; o.answerNode = t; }
  }
}

if (asJson) {
  console.log(JSON.stringify({ question: q, ...o }, null, 2));
} else {
  const intentMap = { "1": "查知识库", "2": "直接回答", "3": "链接入库" };
  console.log("问题: " + q);
  console.log("意图: " + (intentMap[o.intent] || o.intent || "?") +
              (o.topic ? "   主题: " + o.topic : "") +
              (o.hits ? "   检索: " + o.hits + " 条" : ""));
  console.log("─".repeat(70));
  console.log(o.answer || "（没取到答案）");
}

process.exit(o.answer ? 0 : 2);
