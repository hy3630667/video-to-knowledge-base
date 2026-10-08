// 用完整 body 创建文档（支持自定义分段规则 + 每段标题前缀）
// 用法: node _add-document.mjs <payload.json>
// payload.json: { dataset_id, name, file_id, separator, max_tokens, content? }
import { spawnSync } from "node:child_process";
import fs from "node:fs";

const NODE = process.execPath;
const BASE = "http://localhost";

const payloadPath = process.argv[2];
if (!payloadPath) {
  console.log(JSON.stringify({ ok: false, error: "用法: node _add-document.mjs <payload.json>" }));
  process.exit(1);
}

let p;
try {
  p = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: "读 payload 失败: " + e.message }));
  process.exit(1);
}

const { dataset_id, name, file_id, separator = "\n\n===\n\n", max_tokens = 1200 } = p;

if (!dataset_id || !name || !file_id) {
  console.log(JSON.stringify({ ok: false, error: "缺 dataset_id / name / file_id" }));
  process.exit(1);
}

// ⭐ 完整 KnowledgeConfig（buildKnowledgeDocumentBody 会原样透传对象形式的 body）
const body = {
  name,
  indexing_technique: "high_quality",
  data_source: {
    info_list: {
      data_source_type: "upload_file",
      file_info_list: { file_ids: [file_id] },
    },
  },
  process_rule: {
    mode: "custom",
    rules: {
      pre_processing_rules: [
        { id: "remove_extra_spaces", enabled: true },
        { id: "remove_urls_emails", enabled: false },
      ],
      segmentation: {
        separator,
        max_tokens,
        chunk_overlap: 0,
      },
      parent_mode: "paragraph",
    },
  },
};

const r = spawnSync(NODE, [
  "bin/difywf.js", "knowledge", "create-document",
  "--dataset-id", dataset_id,
  "--body", JSON.stringify(body),
  "--base-url", BASE,
], { encoding: "utf8", cwd: process.cwd(), maxBuffer: 64 * 1024 * 1024 });

const out = (r.stdout || "").trim();
try {
  const j = JSON.parse(out);
  if (j.ok) {
    const doc = (j.data?.documents || [])[0] || {};
    console.log(JSON.stringify({
      ok: true,
      doc_id: doc.id,
      doc_name: doc.name,
      doc_form: doc.doc_form,
      indexing_status: doc.indexing_status,
      separator,
      max_tokens,
    }));
  } else {
    console.log(JSON.stringify({ ok: false, error: j.error }));
  }
} catch (e) {
  console.log(JSON.stringify({
    ok: false,
    error: "返回非 JSON",
    raw: out.slice(0, 400),
    stderr: (r.stderr || "").slice(0, 300),
  }));
}
