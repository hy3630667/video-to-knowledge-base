// 调 Dify 纠错工作流的桥接脚本
// 用法: node _run-correction.mjs <app_id> '<inputs JSON>'
// 输出: {"ok":true,"clean":"...","clean_len":N,"removed_chars":N,"has_think":0}
import { spawnSync } from "node:child_process";

const NODE = process.execPath;
const BASE = "http://localhost";

const appId = process.argv[2];
const inputsJson = process.argv[3];

if (!appId || !inputsJson) {
  console.log(JSON.stringify({ ok: false, error: "用法: node _run-correction.mjs <app_id> '<inputs JSON>'" }));
  process.exit(1);
}

const r = spawnSync(NODE, ["bin/difywf.js", "workflow", "run-draft",
  "--app-id", appId,
  "--inputs", inputsJson,
  "--base-url", BASE],
  { encoding: "utf8", cwd: process.cwd(), maxBuffer: 64 * 1024 * 1024 });

const raw = r.stdout || "";
try {
  const j = JSON.parse(raw);
  if (!j.ok) {
    console.log(JSON.stringify({ ok: false, error: j.error }));
    process.exit(0);
  }
  const evs = Array.isArray(j.data) ? j.data : Object.values(j.data || {});
  const fin = evs.find((e) => (e.event || e.type) === "workflow_finished");
  const outs = fin?.data?.outputs || {};

  // 检查有没有失败节点
  const failed = evs.find((e) => (e.data || {}).status === "failed" && (e.event || e.type) === "node_finished");
  if (failed) {
    console.log(JSON.stringify({
      ok: false,
      error: `节点「${failed.data.title}」失败: ${JSON.stringify(failed.data.error)}`,
    }));
    process.exit(0);
  }

  console.log(JSON.stringify({
    ok: true,
    clean: outs.clean ?? "",
    clean_len: outs.clean_len ?? 0,
    removed_chars: outs.removed_chars ?? 0,
    has_think: outs.has_think ?? 0,
  }));
} catch (e) {
  console.log(JSON.stringify({
    ok: false,
    error: "返回非 JSON: " + raw.slice(0, 300),
    stderr: (r.stderr || "").slice(0, 200),
  }));
}
