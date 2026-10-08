// 从知识库实际内容，重新生成「主题判断」节点的提示词
//
// 为什么要这个脚本：
//   「主题判断」节点靠提示词里的分类描述来判断用户问题属于哪一类。
//   如果描述是凭想象写的、或者跟知识库实际内容对不上，判断就会错。
//   实测教训：一开始描述是手写的泛泛分类，4 道题全判错；
//   改成从知识库读真实标题 + 知识点自动生成后，0/4 → 4/4。
//
// 什么时候跑：
//   · 导入工作流之后（DSL 里的提示词是模板占位符）
//   · 每次往知识库加新视频之后（不加，新视频判不出主题）
//
// 用法：
//   node sync-topic-prompt.mjs
//   node sync-topic-prompt.mjs --dry-run      只看会生成什么，不写入
//   node sync-topic-prompt.mjs --publish      写完顺便发布应用
//
// 配置来源（按顺序找）：
//   1. 环境变量：DIFY_BASE / DEFAULT_DATASET / QA_APP_ID / TOPIC_METADATA_ID
//   2. 同目录的 config.py（python 语法，用正则读，不执行）
//   3. 命令行参数：--dataset <id> --app <id> --base <url> --node <节点id>
//
// ⚠️ 这个脚本直接调 Dify 的 console API，需要 console cookie。
//    cookie 从 ~/.difywf/hosts.json 读（dify-mcp 存的位置）。
//    如果你不用 dify-mcp，改成读你自己的凭据即可。

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";

// ---------- 参数 ----------
const argv = process.argv.slice(2);
const flag = (name) => argv.includes("--" + name);
const opt = (name, dflt = "") => {
  const i = argv.indexOf("--" + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
};

const DRY = flag("dry-run");
const PUBLISH = flag("publish");
const NODE_ID = opt("node", "llm_topic");

// ---------- 配置 ----------
function readConfigPy() {
  const p = path.join(process.cwd(), "config.py");
  if (!fs.existsSync(p)) return {};
  const t = fs.readFileSync(p, "utf8");
  const get = (k) => {
    const m = t.match(new RegExp("^" + k + "\\s*=\\s*[\"']([^\"']*)[\"']", "m"));
    return m ? m[1] : "";
  };
  return {
    DIFY_BASE: get("DIFY_BASE"),
    DEFAULT_DATASET: get("DEFAULT_DATASET"),
    QA_APP_ID: get("QA_APP_ID"),
    TOPIC_METADATA_ID: get("TOPIC_METADATA_ID"),
    DIFY_MCP_DIR: get("DIFY_MCP_DIR"),
  };
}

const cfg = readConfigPy();
const BASE = opt("base", process.env.DIFY_BASE || cfg.DIFY_BASE || "http://localhost");
const DS = opt("dataset", process.env.DEFAULT_DATASET || cfg.DEFAULT_DATASET);
const APP = opt("app", process.env.QA_APP_ID || cfg.QA_APP_ID);
const DIFY_MCP = process.env.DIFY_MCP_DIR || cfg.DIFY_MCP_DIR || path.join(process.cwd(), "..", "dify-mcp");

if (!DS || !APP) {
  console.error("❌ 缺配置。请在 config.py 里填 DEFAULT_DATASET 和 QA_APP_ID，");
  console.error("   或用命令行指定：--dataset <知识库ID> --app <应用ID>");
  process.exit(1);
}

// ---------- 读 cookie（dify-mcp 的存储位置）----------
function loadCookies(base) {
  const hf = path.join(os.homedir(), ".difywf", "hosts.json");
  if (!fs.existsSync(hf)) return null;
  try {
    const raw = JSON.parse(fs.readFileSync(hf, "utf8"));
    const entry = raw.hosts?.[base] ?? raw.hosts?.[base + "/"] ?? Object.values(raw.hosts || {})[0];
    return entry?.console_cookies ?? null;
  } catch { return null; }
}

const cookies = loadCookies(BASE);
if (!cookies) {
  console.error("❌ 找不到 Dify console cookie（~/.difywf/hosts.json）");
  console.error("   这个脚本直接调 console API，需要登录态。");
  console.error("   如果你用 dify-mcp：difywf auth login-console --email <你的邮箱>");
  process.exit(1);
}

const cookieHeader = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
const csrf = cookies.csrf_token || "";

// ---------- HTTP 助手 ----------
async function api(method, p, body) {
  const res = await fetch(BASE + p, {
    method,
    headers: {
      "Content-Type": "application/json",
      Cookie: cookieHeader,
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(20000),
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${typeof data === "string" ? data : JSON.stringify(data).slice(0, 200)}`);
  return data;
}

// ---------- 1. 读知识库：文档 + 主题 + 知识点 ----------
console.log("读取知识库…");
const dl = await api("GET", `/console/api/datasets/${DS}/documents?page=1&limit=100`);
const docs = dl.data ?? dl.documents ?? [];

const byTopic = {};
for (const d of docs) {
  const topic = (d.doc_metadata || []).find((m) => m.name === "主题")?.value || "未分类";

  let pts = [];
  try {
    const sl = await api("GET", `/console/api/datasets/${DS}/documents/${d.id}/segments?page=1&limit=20`);
    const segs = sl.data ?? sl.segments ?? [];
    const all = segs.map((s) => String(s.content || "")).join("\n");
    // 抓「知识点」列表里的条目
    pts = [...all.matchAll(/^-\s*(.{4,40}?)[，。；\n]/gm)].map((m) => m[1].trim());
  } catch { /* 拿不到分段就用空 */ }

  const title = String(d.name || "").replace(/^(bilibili|douyin|xhs|youtube)-/, "").slice(0, 48);
  if (!byTopic[topic]) byTopic[topic] = [];
  byTopic[topic].push({ title, points: [...new Set(pts)].slice(0, 8) });
}

const docCount = Object.values(byTopic).reduce((a, v) => a + v.length, 0);
const topics = Object.keys(byTopic);
console.log(`  读到 ${docCount} 个文档，分 ${topics.length} 类：`);
for (const t of topics) {
  console.log(`    【${t}】`);
  for (const v of byTopic[t]) {
    console.log(`      《${v.title}》  (${v.points.length} 条知识点)`);
  }
}

if (docCount === 0) {
  console.error("\n❌ 知识库里没有文档，或都没打「主题」标签。");
  console.error("   先入库几个视频，再给文档打主题标签。");
  process.exit(1);
}

// ---------- 2. 生成提示词 ----------
const lines = [
  "判断用户的问题跟下面哪一类内容有关，输出类别名。",
  "",
  "知识库当前收录了这些视频：",
  "",
];
for (const t of topics) {
  for (const v of byTopic[t]) {
    lines.push(`【${t}】《${v.title}》`);
    for (const p of v.points) lines.push(`   · ${p}`);
    lines.push("");
  }
}
lines.push(
  "输出规则（必须严格遵守）：",
  `只输出上面 ${topics.length} 个类别名中的一个，不要标点、不要解释、不要换行、不要空格：`,
  ...topics,
  "",
  "判断方法：看用户问的话题最接近上面哪一条「·」内容。",
  "特别注意：",
  "· 问「工作流怎么搭」「提示词怎么写」「某个工具怎么用」时，",
  "  要看这个工具/工作流属于哪一类 —— 不要因为出现「工作流」三个字就都归到工具类。",
  "· 如果问题提到具体的视频名、UP 主名、或上面列出的关键词，直接归到那条内容所属的类别。",
  `· 完全看不出属于哪类时，输出：${topics[0]}`,
  "",
  "=== 用户的问题 ===",
  "{{#sys.query#}}",
);

const prompt = lines.join("\n");
console.log(`\n生成提示词 ${prompt.length} 字`);

if (DRY) {
  console.log("\n--- 提示词全文（--dry-run，不写入）---");
  console.log(prompt);
  process.exit(0);
}

// ---------- 3. 读草稿图，改 llm_topic 节点 ----------
console.log("\n读取工作流草稿…");
const node = process.execPath;
const cli = path.join(DIFY_MCP, "bin", "difywf.js");
if (!fs.existsSync(cli)) {
  console.error(`❌ 找不到 dify-mcp CLI: ${cli}`);
  console.error("   在 config.py 里设 DIFY_MCP_DIR，或从 https://github.com/ 获取 dify-mcp");
  process.exit(1);
}

const r = spawnSync(node, [cli, "workflow", "get-draft", "--app-id", APP, "--base-url", BASE],
  { encoding: "utf8", cwd: DIFY_MCP, maxBuffer: 64 * 1024 * 1024 });
let g;
try {
  g = JSON.parse((r.stdout || "").replace(/^\uFEFF/, "")).data.graph;
} catch {
  console.error("❌ 读草稿失败:", (r.stdout || r.stderr || "").slice(0, 300));
  process.exit(1);
}

const target = g.nodes.find((n) => n.id === NODE_ID);
if (!target) {
  console.error(`❌ 找不到节点 ${NODE_ID}`);
  console.error("   现有节点：", g.nodes.map((n) => n.id).join(", "));
  process.exit(1);
}

const oldLen = target.data.prompt_template[0].text.length;
target.data.prompt_template[0].text = prompt;
console.log(`  ${NODE_ID} 的提示词: ${oldLen} → ${prompt.length} 字`);

// ---------- 4. 写回 ----------
const tmp = path.join(os.tmpdir(), `_graph-topic-${Date.now()}.json`);
fs.writeFileSync(tmp, JSON.stringify(g, null, 2), "utf8");

console.log("同步到 Dify…");
const s = spawnSync(node, [cli, "workflow", "draft", "sync",
  "--app-id", APP, "--graph", tmp, "--confirm", "--base-url", BASE],
  { encoding: "utf8", cwd: DIFY_MCP, maxBuffer: 64 * 1024 * 1024 });

let sj;
try { sj = JSON.parse(s.stdout || "{}"); } catch { sj = { ok: false, error: { message: (s.stdout || "").slice(0, 200) } }; }

if (!sj.ok) {
  console.error("❌ 同步失败:", JSON.stringify(sj.error).slice(0, 300));
  process.exit(1);
}
console.log("  ✅ 已同步");

// ---------- 5. 可选发布 ----------
if (PUBLISH) {
  console.log("发布应用…");
  const p = spawnSync(node, [cli, "workflow", "publish", "--app-id", APP, "--confirm", "--base-url", BASE],
    { encoding: "utf8", cwd: DIFY_MCP, maxBuffer: 64 * 1024 * 1024 });
  try {
    const pj = JSON.parse(p.stdout || "{}");
    console.log(pj.ok ? "  ✅ 已发布" : "  ❌ 发布失败: " + JSON.stringify(pj.error).slice(0, 200));
  } catch { console.log("  ⚠️ 发布结果解析失败"); }
} else {
  console.log("\n⭐ 提示词已改，但应用还没发布。");
  console.log("   改完草稿要发布才生效：加 --publish 参数重跑，");
  console.log("   或去 Dify 控制台点「发布」。");
}

console.log(`\n完成：${docCount} 个文档 / ${topics.length} 个类别 → 提示词 ${prompt.length} 字`);
