# -*- coding: utf-8 -*-
"""
视频入库服务（零依赖，Python 标准库 http.server）

用途：让员工在对话里发一个视频链接，服务在后台跑完「下载→转写→纠错→入库」，
      对话侧不用等（提交后立刻拿到 job_id）。

端点：
  GET  /health          健康检查（含 Ollama / Dify / ffmpeg）
  POST /ingest          提交链接  body: {"url": "..."}  → {job_id, status}
  GET  /jobs/<job_id>   查单个任务状态
  GET  /jobs            最近 20 个任务

启动：python ingest-service.py            （默认 0.0.0.0:8090）
      python ingest-service.py 8091       （换端口）
"""
import io
import json
import os
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---- 复用一键脚本的配置与函数 ----
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# one-click-pipeline.py 在模块级会重包装 stdout，这里先保证它有 buffer
if not hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace")

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "pipeline", os.path.join(HERE, "one-click-pipeline.py")
)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

# ---- 任务表 ----
JOBS = {}
LOCK = threading.Lock()
STATE_FILE = os.path.join(HERE, "_ingest-jobs.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090


def _load_jobs():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                JOBS.update(json.load(f))
        except Exception:
            pass


def _save_jobs():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(JOBS, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _patch(job_id, **kw):
    with LOCK:
        JOBS.setdefault(job_id, {})
        JOBS[job_id].update(kw)
        _save_jobs()


def _log(job_id, msg):
    with LOCK:
        j = JOBS.setdefault(job_id, {})
        j.setdefault("logs", []).append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        j["logs"] = j["logs"][-40:]          # 只留最后 40 条
        _save_jobs()


# ==================== 后台入库流程 ====================
def run_ingest(job_id, url, dataset_id):
    t0 = time.time()
    _patch(job_id, status="running", stage="下载中", progress=5)
    _log(job_id, f"开始处理 {url}")

    try:
        # 1. 下载
        info = P.download_video(url)
        if not info:
            raise RuntimeError("下载失败：链接可能失效，或平台不支持的格式")
        title = info.get("title") or "(无标题)"
        _patch(job_id, title=title, duration=round(info.get("duration") or 0, 1),
               stage="转写中", progress=25)
        _log(job_id, f"下载完成：{title}（{info.get('duration', 0):.0f} 秒）")

        # 2. 转写
        tr = P.transcribe(info["file"], "small")
        if not tr:
            raise RuntimeError("转写失败：视频可能没有音轨")
        _patch(job_id, stage="纠错中", progress=60,
               words=len(tr.get("text") or ""))
        _log(job_id, f"转写完成：{len(tr.get('text') or '')} 字，{tr.get('ratio', 0):.2f}x")

        # 3. 纠错
        corrected = None
        try:
            corrected = P.correct_transcript(info, tr)
        except Exception as e:
            _log(job_id, f"纠错失败（用原始稿继续）：{e}")

        # 4. 写知识库
        _patch(job_id, stage="入库中", progress=80)
        result = P.write_to_dify(info, tr, dataset_id, corrected)
        if not result:
            raise RuntimeError("写入知识库失败")

        _patch(job_id, status="done", stage="已完成", progress=100,
               doc_id=result.get("doc_id"), doc_name=result.get("doc_name"),
               elapsed=round(time.time() - t0, 1),
               message=f"《{title}》已入库")
        _log(job_id, f"✅ 入库完成，文档 {result.get('doc_id')}，耗时 {time.time()-t0:.1f} 秒")

    except Exception as e:
        tb = traceback.format_exc(limit=3)
        _patch(job_id, status="failed", stage="失败", progress=100,
               error=str(e), elapsed=round(time.time() - t0, 1),
               message=f"处理失败：{e}")
        _log(job_id, f"❌ {e}\n{tb}")


# ==================== 健康检查 ====================
def health():
    checks = {}

    # Ollama
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as r:
            data = json.loads(r.read().decode())
        names = [m.get("name", "") for m in data.get("models", [])]
        checks["ollama"] = {"ok": True, "models": names}
    except Exception as e:
        checks["ollama"] = {"ok": False, "error": str(e)}

    # Dify
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost/console/api/setup", timeout=4) as r:
            checks["dify"] = {"ok": r.status == 200}
    except Exception as e:
        checks["dify"] = {"ok": False, "error": str(e)}

    # ffmpeg / node
    checks["ffmpeg"] = {"ok": os.path.isfile(P.FFMPEG or ""), "path": P.FFMPEG}
    checks["node"] = {"ok": os.path.isfile(P.NODE or ""), "path": P.NODE}

    # 抓取服务
    try:
        import urllib.request
        with urllib.request.urlopen(P.DOUYIN_API, timeout=4) as r:
            checks["douyin_api"] = {"ok": r.status < 500}
    except Exception as e:
        checks["douyin_api"] = {"ok": False, "error": str(e)}

    return {"ok": all(c.get("ok") for c in checks.values()), "checks": checks}


# ==================== HTTP ====================
class Handler(BaseHTTPRequestHandler):
    server_version = "IngestService/1.0"

    def log_message(self, fmt, *args):
        pass   # 静音，自己记

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            return self._send(200, health())

        if path == "/jobs":
            with LOCK:
                items = sorted(JOBS.values(), key=lambda x: x.get("created_at", 0), reverse=True)
            return self._send(200, {"jobs": items[:20]})

        if path.startswith("/jobs/"):
            jid = path[len("/jobs/"):]
            with LOCK:
                job = JOBS.get(jid)
            if not job:
                return self._send(404, {"error": "job not found", "job_id": jid})
            return self._send(200, job)

        return self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/ingest":
            return self._send(404, {"error": "not found", "path": path})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        url = (body.get("url") or "").strip()
        if not url:
            return self._send(400, {"error": "missing url"})
        dataset_id = body.get("dataset_id") or P.DEFAULT_DATASET

        job_id = uuid.uuid4().hex[:12]
        with LOCK:
            JOBS[job_id] = {
                "job_id": job_id, "url": url, "dataset_id": dataset_id,
                "status": "queued", "stage": "排队中", "progress": 0,
                "created_at": time.time(), "logs": [],
            }
            _save_jobs()

        threading.Thread(target=run_ingest, args=(job_id, url, dataset_id), daemon=True).start()

        return self._send(202, {
            "job_id": job_id, "status": "queued",
            "message": "已收到，正在后台处理",
            "poll": f"/jobs/{job_id}",
        })


def main():
    _load_jobs()
    print("=" * 62)
    print("  视频入库服务")
    print("=" * 62)
    print(f"  监听    : http://0.0.0.0:{PORT}")
    print(f"  端点    : GET /health  POST /ingest  GET /jobs/<id>")
    print(f"  知识库  : {P.DEFAULT_DATASET}")
    print(f"  工作目录: {P.WORK_DIR}")
    print()

    h = health()
    for name, c in h["checks"].items():
        mark = "OK  " if c.get("ok") else "FAIL"
        extra = ""
        if name == "ollama" and c.get("models"):
            extra = "  " + ", ".join(c["models"])
        elif not c.get("ok"):
            extra = "  " + str(c.get("error", ""))[:60]
        print(f"  [{mark}] {name}{extra}")
    print()
    print("  按 Ctrl+C 停止")
    print()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
