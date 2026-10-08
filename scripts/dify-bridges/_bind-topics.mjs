// 给视频文档绑定「主题」元数据（带 token 自动刷新）
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { apiCall } from "./src/core/http.ts";
import { csrfValue } from "./src/core/cookies.ts";
import { refreshConsoleCookies, storeCookies } from "./src/core/auth.ts";

const BASE = "http://localhost";
const DS = process.env.DIFY_DATASET_ID || "<你的知识库 ID>";
const META_ID = process.env.DIFY_TOPIC_METADATA_ID || "<主题元数据字段 ID>";

const hostsFile = path.join(os.homedir(), ".difywf", "hosts.json");
const raw = JSON.parse(fs.readFileSync(hostsFile, "utf8"));
const entry = raw.hosts?.[BASE] ?? Object.values(raw.hosts || {})[0];
let cookies = entry?.console_cookies ?? {};

// ---- 0. 先刷新 token（CLI 会自动刷新，裸调 apiCall 不会，所以这里手动刷）----
console.log("=== 0. 刷新 console token ===");
const rf = await refreshConsoleCookies(BASE, cookies);
if (rf.ok) {
  cookies = rf.data;
  storeCookies(BASE, cookies);
  console.log("  ✅ 已刷新并落盘");
} else {
  console.log("  ⚠️ 刷新失败（继续用旧 token 试）:", JSON.stringify(rf.error).slice(0, 120));
}
const csrf = csrfValue(cookies);

// ---- 1. 列文档 ----
console.log("\n=== 1. 列出现有文档 ===");
const list = await apiCall(BASE, `/console/api/datasets/${DS}/documents`, {
  cookies, csrfToken: csrf, query: { page: 1, limit: 50 },
});
if (!list.ok) { console.log("  失败:", JSON.stringify(list.error).slice(0, 160)); process.exit(1); }

const docs = list.data?.data ?? [];
console.log(`  共 ${docs.length} 个文档`);

// ---- 2. 判主题 ----
// ⚠️ 顺序有讲究：靠前的规则先命中。
//    「书籍转技能」类文档名里也可能带 Skill，所以 SKILL 规则必须排在通用规则前面。
// ⚠️ 加了新分类后，**必须跑 _fix-topic-prompt.mjs** 把新分类写进主题判断提示词，
//    否则该主题的文档会被检索的元数据过滤挡掉（查不到）。
const RULES = [
  { kw: ["剧本", "编剧"], topic: "AI编剧" },
  { kw: ["书籍转技能", "SKILL", "Skill"], topic: "AI应用" },
  { kw: ["ComfyUI", "Agent"], topic: "AI工具" },
  { kw: ["MiniMax", "显存", "H3"], topic: "AI视频" },
  { kw: ["电商", "详情页"], topic: "电商运营" },
];
const topicOf = name => (RULES.find(r => r.kw.some(k => name.includes(k)))?.topic) ?? "其他";

const operation_data = docs.map(d => ({
  document_id: d.id,
  metadata_list: [{ id: META_ID, name: "主题", value: topicOf(d.name) }],
}));
for (const o of operation_data) {
  const doc = docs.find(d => d.id === o.document_id);
  console.log(`  ${o.metadata_list[0].value.padEnd(6)} ← ${doc.name.slice(0, 40)}`);
}

// ---- 3. 提交 ----
console.log("\n=== 2. 提交绑定 ===");
const res = await apiCall(BASE, `/console/api/datasets/${DS}/documents/metadata`, {
  method: "POST", cookies, csrfToken: csrf, body: { operation_data },
});
console.log("  ok =", res.ok);
if (!res.ok) console.log("  error =", JSON.stringify(res.error).slice(0, 200));
else console.log("  ✅ 绑定成功");

// ---- 4. 回读 ----
console.log("\n=== 3. 回读验证 ===");
const back = await apiCall(BASE, `/console/api/datasets/${DS}/documents`, {
  cookies, csrfToken: csrf, query: { page: 1, limit: 50 },
});
if (back.ok) {
  for (const d of back.data?.data ?? []) {
    const t = (d.doc_metadata ?? []).map(m => `${m.name}=${m.value}`).join(", ") || "(无)";
    console.log(`  ${t.padEnd(12)} ${d.name.slice(0, 38)}`);
  }
}
