// 把视频链接提交进知识库，并轮询到达（skill 专用）
//
// 用法：
//   node ingest.mjs "https://v.douyin.com/xxxxx/"
//   node ingest.mjs --no-wait "https://..."      只提交，不轮询
//   node ingest.mjs --json "https://..."         输出 JSON
//
// 做的事：查健康 → 提交 → 轮询 → 打印结果
// 退出码：0 成功 / 1 参数或服务错误 / 2 入库失败

const SVC = "http://127.0.0.1:8090";

const args = process.argv.slice(2);
const noWait = args.includes("--no-wait");
const asJson = args.includes("--json");
const url = args.filter(a => !a.startsWith("--")).join(" ").trim();

if (!url) {
  console.error('用法: node ingest.mjs "视频链接"');
  process.exit(1);
}

async function get(p, ms = 10000) {
  const res = await fetch(SVC + p, { signal: AbortSignal.timeout(ms) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ---- 1. 查健康 ----
let health;
try {
  health = await get("/health", 8000);
} catch (e) {
  console.error("❌ 入库服务没响应（" + e.message + "）");
  console.error("   启动：powershell -File \"<你的工作目录>\\入库服务.ps1\" start");
  process.exit(1);
}

const bad = Object.entries(health.checks || {})
  .filter(([, v]) => !v.ok)
  .map(([k, v]) => `${k}${v.error ? "(" + String(v.error).slice(0, 50) + ")" : ""}`);

if (bad.length) {
  console.error("❌ 依赖项异常: " + bad.join(", "));
  console.error("   见 references/01-排障手册.md");
  if (!health.ok) process.exit(1);
}

// ---- 2. 提交 ----
let job;
try {
  const res = await fetch(SVC + "/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
    signal: AbortSignal.timeout(20000),
  });
  job = await res.json();
} catch (e) {
  console.error("❌ 提交失败: " + e.message);
  process.exit(1);
}

if (!asJson) {
  console.log("已受理");
  console.log("  链接    " + url);
  console.log("  任务号  " + job.job_id);
  console.log("");
}

if (noWait) {
  if (asJson) console.log(JSON.stringify({ url, ...job }, null, 2));
  process.exit(0);
}

// ---- 3. 轮询 ----
const t0 = Date.now();
let last = "";
const MAX_MIN = 25;   // 最长等 25 分钟（约覆盖 1.5 小时视频）

while (true) {
  await new Promise(r => setTimeout(r, 6000));
  let j;
  try { j = await get(`/jobs/${job.job_id}`, 10000); }
  catch { continue; }

  const line = `${j.status} ${j.stage} ${j.progress}%`;
  if (line !== last && !asJson) {
    const sec = Math.round((Date.now() - t0) / 1000);
    console.log(`  [${String(sec).padStart(4)}s] ${line}`);
    last = line;
  }

  if (j.status === "done" || j.status === "failed") {
    if (asJson) {
      console.log(JSON.stringify({ url, ...j }, null, 2));
    } else {
      console.log("");
      if (j.status === "done") {
        console.log("已入库");
        console.log("  标题  " + (j.title || ""));
        console.log("  时长  " + (j.duration ?? "?") + " 秒");
        console.log("  字数  " + (j.words ?? "?") + " 字");
        console.log("  耗时  " + (j.elapsed ?? "?") + " 秒");
        console.log("  文档  " + (j.doc_id || ""));
        console.log("");
        console.log("⚠️ 新文档还没有主题标签 —— 不打标签检索会过滤掉它。");
        console.log("   打标签：cd " + "<你的 dify-mcp 目录>" +
                    " && node _bind-topics.mjs");
      } else {
        console.log("❌ 入库失败");
        console.log("  错误  " + (j.error || "(无 error 字段)"));
        if (j.logs?.length) {
          console.log("  日志  " + j.logs.slice(-3).join("\n        "));
        }
      }
    }
    process.exit(j.status === "done" ? 0 : 2);
  }

  if ((Date.now() - t0) / 60000 > MAX_MIN) {
    console.error(`\n⚠️ 等了 ${MAX_MIN} 分钟还没完，不再轮询。`);
    console.error(`   之后自己查：curl ${SVC}/jobs/${job.job_id}`);
    process.exit(3);
  }
}
