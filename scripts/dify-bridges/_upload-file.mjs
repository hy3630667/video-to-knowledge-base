// 上传文件到 Dify —— 从**文件**读 payload，绕开 Windows 命令行长度上限
//
// 为什么需要它（2026-10-08 实测）：
//   one-click-pipeline.py 原来把 {name, content_b64, mime} 直接当**命令行参数**传给
//   `difywf.js file upload --file <json>`。内容一大，base64 就爆：
//     9178 字中文 → UTF-8 ≈27KB → base64 ≈36,700 字符 > Windows 上限 32,767
//   → `FileNotFoundError: [WinError 206] 文件名或扩展名太长`，整篇入不了库。
//
//   实测 CLI 的 `--file` **不支持** `@file` 或 `-`(stdin)（只认内联 JSON），
//   所以这里直接调 console API 的 `POST /console/api/files/upload`（multipart）。
//
// 用法：node _upload-file.mjs <payload.json>
//   payload.json: { name, content_b64, mime? }
// 输出：{"ok":true,"data":{"id":"<file_id>"},...}  —— 与 CLI 返回同形，便于替换
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { apiCall } from "./src/core/http.ts";
import { csrfValue } from "./src/core/cookies.ts";
import { refreshConsoleCookies, storeCookies } from "./src/core/auth.ts";
import { toFormData, isFilePayload } from "./src/core/multipart.ts";

const BASE = "http://localhost";
const payloadPath = process.argv[2];

function die(msg) {
  console.log(JSON.stringify({ ok: false, error: msg }));
  process.exit(1);
}

if (!payloadPath) die("用法: node _upload-file.mjs <payload.json>");
let payload;
try {
  payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
} catch (e) {
  die("读 payload 失败: " + e.message);
}
if (!isFilePayload(payload)) die("payload 必须是 {name, content_b64, mime?}");

// 裸调 apiCall 不会自动刷新 token（坑 26）
const hf = path.join(os.homedir(), ".difywf", "hosts.json");
let cookies = (JSON.parse(fs.readFileSync(hf, "utf8")).hosts?.[BASE] ?? {})?.console_cookies ?? {};
const rf = await refreshConsoleCookies(BASE, cookies);
if (rf.ok) { cookies = rf.data; storeCookies(BASE, cookies); }
const csrf = csrfValue(cookies);

const bytes = Buffer.from(payload.content_b64, "base64").length;
const r = await apiCall(BASE, "/console/api/files/upload", {
  method: "POST", cookies, csrfToken: csrf, body: toFormData(payload),
});

if (!r.ok) {
  die(JSON.stringify(r.error).slice(0, 300));
}
console.log(JSON.stringify({
  ok: true,
  data: r.data,
  _bytes: bytes,
  _name: payload.name,
}));
