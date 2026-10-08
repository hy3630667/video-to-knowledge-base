// 导出知识库到 JSON（文档 + 每个文档的全部分段内容）
//
// 用途：
//   ① 合并主题文档（按 C 方案）前，把现有内容拉出来
//   ② 顺带当**知识库备份** —— 重入库/删文档前先存一份
//
// 用法：node _export-docs.mjs [输出路径]   ← 省略则写到当前目录
//   默认 <当前目录>/_kb-export.json
import fs from "node:fs"; import path from "node:path"; import os from "node:os";
import { apiCall } from "./src/core/http.ts";
import { csrfValue } from "./src/core/cookies.ts";
import { refreshConsoleCookies, storeCookies } from "./src/core/auth.ts";

const BASE = "http://localhost";
const DS = process.env.DIFY_DATASET_ID || "<你的知识库 ID>";
const OUT = process.argv[2] || path.join(process.cwd(), "_kb-export.json");

const hf = path.join(os.homedir(), ".difywf", "hosts.json");
let cookies = (JSON.parse(fs.readFileSync(hf, "utf8")).hosts?.[BASE] ?? {})?.console_cookies ?? {};
const rf = await refreshConsoleCookies(BASE, cookies);
if (rf.ok) { cookies = rf.data; storeCookies(BASE, cookies); }
const csrf = csrfValue(cookies);

const dl = await apiCall(BASE, `/console/api/datasets/${DS}/documents`, {
  cookies, csrfToken: csrf, query: { page: 1, limit: 200 },
});
const docs = dl.data?.data || [];
console.log(`导出 ${docs.length} 篇文档…`);

const out = { dataset: DS, when: new Date().toISOString(), documents: [] };
for (const d of docs) {
  const topic = (d.doc_metadata || []).find(m => m.name === "主题")?.value || "";
  const sl = await apiCall(BASE, `/console/api/datasets/${DS}/documents/${d.id}/segments`, {
    cookies, csrfToken: csrf, query: { page: 1, limit: 100 },
  });
  const segs = (sl.data?.data || [])
    .sort((a, b) => (a.position || 0) - (b.position || 0))
    .map(s => ({ position: s.position, content: String(s.content || "") }));
  const chars = segs.reduce((a, s) => a + s.content.length, 0);
  console.log(`  [${topic || "无主题"}] ${d.name.slice(0, 46)}  ${segs.length} 段 / ${chars} 字`);
  out.documents.push({ id: d.id, name: d.name, topic, chars, segments: segs });
}
fs.writeFileSync(OUT, JSON.stringify(out, null, 2), "utf8");
console.log(`\n✅ 已导出 ${OUT}`);
console.log(`   总计 ${out.documents.reduce((a, d) => a + d.chars, 0)} 字`);
