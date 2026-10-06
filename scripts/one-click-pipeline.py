# -*- coding: utf-8 -*-
"""
一键链路：视频链接 -> 下载 -> 转写 -> 写入 Dify 知识库

用法:
    python one-click-pipeline.py "<视频链接>" [--dataset-id <id>] [--model small]

依赖（都已装好）:
    faster-whisper, opencc, ffmpeg, dify-mcp (CLI), Douyin_TikTok_Download_API (Docker)

实测数据（2026-10-05）:
    抖音下载  : 27秒视频 2.56MB / 3.5 秒
    转写      : small 模型 6.73x 实时（1 小时音频约 8.9 分钟）
    写入知识库: file.upload -> create_document -> 索引完成约 5 秒
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ==================== 配置 ====================
# 三级回退：config.py（本地，已 gitignore）→ 环境变量 → 自动探测
#
# 想改配置：复制 config.example.py 为 config.py，填上自己的值。
# 不改也能跑：只要 ffmpeg / node 在 PATH 上。

import shutil

try:
    import config as _cfg          # 本地配置（可选）
except ImportError:
    _cfg = None


def _c(name, default=None, env=None):
    """取配置：config.py → 环境变量 → 默认值"""
    if _cfg is not None and hasattr(_cfg, name):
        v = getattr(_cfg, name)
        if v:
            return v
    e = os.environ.get(env or name)
    if e:
        return e
    return default


def _which(name, fallback=None):
    """在 PATH 上找可执行文件，找不到就用 fallback（用户显式配的路径）"""
    p = shutil.which(name)
    if p:
        return p
    if fallback and os.path.exists(fallback):
        return fallback
    return fallback or name


PYTHON = sys.executable

# 外部命令（优先 PATH，其次 config.py 里的绝对路径）
NODE = _which("node", _c("NODE"))
FFMPEG = _which("ffmpeg", _c("FFMPEG"))
FFPROBE = _which("ffprobe", _c("FFPROBE", default=(
    os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe") if FFMPEG and os.path.dirname(FFMPEG) else None
)))

# dify-mcp 目录（内含 bin/difywf.js）
DIFY_MCP_DIR = _c("DIFY_MCP_DIR", env="DIFY_MCP_DIR")

# 服务地址
DOUYIN_API = _c("DOUYIN_API", "http://localhost:8080")      # Douyin_TikTok_Download_API
XHS_API = _c("XHS_API", "http://127.0.0.1:5556/xhs/detail")  # XHS-Downloader
DIFY_BASE = _c("DIFY_BASE", "http://localhost")

# 小红书 Cookie 文件（含登录凭据，不要提交到 Git）
XHS_COOKIE_FILE = _c("XHS_COOKIE_FILE", os.path.join(os.path.expanduser("~"), ".xhs-cookie.txt"))

# 工作目录（下载的视频、临时音频都放这）
WORK_DIR = _c("WORK_DIR", os.path.join(os.path.expanduser("~"), "video-kb-work"))

# 目标知识库 ID（在 Dify 知识库页面 URL 里能看到）
DEFAULT_DATASET = _c("DEFAULT_DATASET", env="DIFY_DATASET_ID")

# Dify 工作流「转写稿纠错与知识点提取」的 app id
# 从 workflows/01-transcript-correction.yml 导入后，在应用 URL 里能看到
CORRECTION_APP_ID = _c("CORRECTION_APP_ID", env="CORRECTION_APP_ID")

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

os.makedirs(WORK_DIR, exist_ok=True)


def log(msg=""):
    print(msg, flush=True)


def hr(title=""):
    log("=" * 72)
    if title:
        log(title)
        log("=" * 72)


def probe_duration(media):
    """用 ffprobe 拿真实时长（比 API 可靠；小红书 API 不返回时长）"""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", media],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


# ==================== 1c. B站下载 ====================
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BILI_HEADERS = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}


def _bili_get(path, params):
    """调 dtk 的 B站端点"""
    u = f"{DOUYIN_API}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(u, timeout=90) as r:
        return json.loads(r.read().decode())


def _resolve_short_link(url):
    """解析 b23.tv 短链，返回最终 URL"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BILI_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.geturl()
    except Exception:
        return url


def _fetch_file(url, out_path, timeout=300):
    """下载文件（带 B站 防盗链头）"""
    req = urllib.request.Request(url, headers=BILI_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(out_path)


def download_bilibili(url):
    """
    B站：dash 分离流，必须分别下视频和音频再用 ffmpeg 合并

    ⚠️ 两个陷阱：
    1. dtk 的 /api/hybrid/video_data 不支持 B站（会 400），必须走 /api/bilibili/*
    2. durl（合并流）返回 0 条，只能走 dash；且 m4s 需要 Referer 防盗链头
    """
    hr("STEP 1: 下载视频（B站）")
    log(f"  链接: {url}")
    if "b23.tv" in url:
        url = _resolve_short_link(url)
        log(f"  短链已解析: {url[:100]}")

    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        log("  ❌ 从链接里提不到 BV 号")
        return None
    bv = m.group(1)
    log(f"  BV 号: {bv}")

    # 1a. 视频信息
    try:
        info_res = _bili_get("/api/bilibili/web/fetch_one_video", {"bv_id": bv})
    except Exception as e:
        log(f"  ❌ 取视频信息失败: {e}")
        return None
    d = (info_res.get("data") or {}).get("data") or {}
    if not d:
        log(f"  ❌ 返回无 data: {json.dumps(info_res, ensure_ascii=False)[:250]}")
        return None

    title = (d.get("title") or "").strip()[:60]
    desc = (d.get("desc") or "").strip()
    cid = d.get("cid")
    dur_ms = d.get("duration") or 0
    owner = d.get("owner") or {}
    stat = d.get("stat") or {}
    log(f"  平台    : bilibili")
    log(f"  标题    : {title}")
    log(f"  UP 主   : {owner.get('name')} ({owner.get('mid')})")
    log(f"  时长    : {dur_ms} 秒")
    log(f"  数据    : 播放 {stat.get('view')}  点赞 {stat.get('like')}  "
        f"收藏 {stat.get('favorite')}  评论 {stat.get('reply')}")
    log(f"  描述    : {desc[:100]}")

    if not cid:
        log("  ❌ 没有 cid，无法取播放地址")
        return None

    # 1b. 播放地址
    log("  取播放地址（dash）...")
    try:
        play_res = _bili_get("/api/bilibili/web/fetch_video_playurl", {"bv_id": bv, "cid": cid})
    except Exception as e:
        log(f"  ❌ 取播放地址失败: {e}")
        return None
    pd = (play_res.get("data") or {}).get("data") or {}
    dash = pd.get("dash") or {}
    vids = dash.get("video") or []
    auds = dash.get("audio") or []
    if not vids:
        log("  ❌ dash 里没有视频流（可能需要登录 Cookie 才能拿高清晰度）")
        return None

    # 优先 H.264（兼容性最好），按带宽排序
    def pick(arr, prefer_avc=True):
        if not arr:
            return None
        if prefer_avc:
            avc = [x for x in arr if str(x.get("codecs", "")).startswith("avc")]
            if avc:
                return max(avc, key=lambda x: x.get("bandwidth") or 0)
        return max(arr, key=lambda x: x.get("bandwidth") or 0)

    v = pick(vids)
    a = pick(auds, prefer_avc=False)
    log(f"  视频流: {v.get('width')}x{v.get('height')} "
        f"codec={str(v.get('codecs'))[:20]} bw={v.get('bandwidth')}")
    if a:
        log(f"  音频流: codec={str(a.get('codecs'))[:20]} bw={a.get('bandwidth')}")

    # 1c. 下载两条流
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:50]
    out_file = os.path.join(WORK_DIR, f"bilibili_{bv}_{safe}.mp4")
    if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
        log(f"  ✅ 已存在，跳过下载: {out_file}")
    else:
        vtmp = os.path.join(WORK_DIR, f"_bili_{bv}_v.m4s")
        atmp = os.path.join(WORK_DIR, f"_bili_{bv}_a.m4s")
        t0 = time.time()
        try:
            sz_v = _fetch_file(v["baseUrl"], vtmp)
            log(f"    视频流已下: {sz_v:,} 字节")
            sz_a = 0
            if a:
                sz_a = _fetch_file(a["baseUrl"], atmp)
                log(f"    音频流已下: {sz_a:,} 字节")
        except Exception as e:
            log(f"  ❌ 流下载失败: {type(e).__name__} {e}")
            return None

        # 1d. ffmpeg 合并
        log("  ffmpeg 合并音视频...")
        if a and sz_a > 0:
            cmd = [FFMPEG, "-y", "-i", vtmp, "-i", atmp, "-c", "copy", out_file]
        else:
            cmd = [FFMPEG, "-y", "-i", vtmp, "-c", "copy", out_file]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        for f in (vtmp, atmp):
            try:
                os.remove(f)
            except Exception:
                pass
        if r.returncode != 0 or not os.path.exists(out_file):
            log(f"  ❌ 合并失败: {r.stderr[-400:]}")
            return None
        log(f"  ✅ 下载完成: {os.path.getsize(out_file):,} 字节，"
            f"耗时 {time.time()-t0:.1f} 秒")

    dur = probe_duration(out_file) or (dur_ms or 0)
    if dur:
        log(f"  实际时长: {dur:.1f} 秒（ffprobe）")

    full_desc = (f"{desc}\n\nUP主: {owner.get('name')}\n"
                 f"互动数据: 播放 {stat.get('view')}  点赞 {stat.get('like')}  "
                 f"收藏 {stat.get('favorite')}  评论 {stat.get('reply')}  投币 {stat.get('coin')}")
    return {"file": out_file, "title": title, "desc": full_desc,
            "platform": "bilibili", "aweme_id": bv, "duration": dur}


# ==================== 1b. 小红书下载 ====================
def _scrub(obj):
    """⚠️ XHS API 会把 cookie 原样回显在 params 里，必须递归清掉再打印"""
    SENSITIVE = {"cookie", "id_token", "web_session", "a1"}
    if isinstance(obj, dict):
        return {k: ("<已隐藏>" if k.lower() in SENSITIVE else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def download_xhs(url):
    """
    小红书：调 XHS-Downloader 的 REST API

    ⚠️ 两个陷阱：
    1. 链接必须带 xsec_token，否则返回「获取小红书作品数据失败」
       （从 App 分享出来的链接通常自带；从网页复制要带上 ?xsec_token=...）
    2. API 会把 cookie 回显在响应的 params 里 —— 打印前必须过滤
    """
    hr("STEP 1: 下载视频（小红书）")
    log(f"  链接: {url}")

    if "xsec_token=" not in url:
        log("  ⚠️ 链接里没有 xsec_token，小红书很可能返回失败")
        log("     请在网页端打开笔记后复制完整链接（含 ?xsec_token=...）")

    if not os.path.exists(XHS_COOKIE_FILE):
        log(f"  ❌ 找不到 Cookie 文件: {XHS_COOKIE_FILE}")
        log("     需要用 Playwright 登录小红书后导出")
        return None
    cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
    log(f"  Cookie: 已读取（{len(cookie)} 字符）")

    body = json.dumps({"url": url, "download": False, "cookie": cookie}).encode("utf-8")
    req = urllib.request.Request(XHS_API, data=body,
                                 headers={"Content-Type": "application/json"})
    log("  调 XHS-Downloader API...")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        log(f"  ❌ API 失败: {e}")
        return None

    if res.get("message") != "获取小红书作品数据成功" or not res.get("data"):
        log(f"  ❌ 抓取失败: {res.get('message')}")
        log(f"     响应（已脱敏）: {json.dumps(_scrub(res), ensure_ascii=False)[:400]}")
        return None

    d = res["data"]
    title = (d.get("作品标题") or "").strip()
    desc = (d.get("作品描述") or "").strip()
    aweme_id = d.get("作品ID") or "unknown"
    if not title:
        # 无标题时用描述首句
        title = re.split(r"[#\n。！？]", desc)[0].strip() or f"xhs-{aweme_id}"
    title = title[:60]

    # 发布时间形如 2026-08-12_08:17:10
    pub = str(d.get("发布时间") or "").replace("_", " ")
    stats = {
        "点赞": d.get("点赞数量"), "收藏": d.get("收藏数量"),
        "评论": d.get("评论数量"), "分享": d.get("分享数量"),
    }

    log(f"  平台    : xiaohongshu")
    log(f"  ID      : {aweme_id}")
    log(f"  标题    : {title}")
    log(f"  作者    : {d.get('作者昵称')} ({d.get('作者ID')})")
    log(f"  发布    : {pub}")
    log(f"  类型    : {d.get('作品类型')}")
    log(f"  数据    : " + "  ".join(f"{k}={v}" for k, v in stats.items() if v is not None))
    log(f"  标签    : {d.get('作品标签')}")
    log(f"  描述    : {desc[:100]}")

    # 下载视频
    links = d.get("下载地址") or []
    links = [x for x in links if isinstance(x, str) and x.startswith("http")]
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:50]
    out_file = os.path.join(WORK_DIR, f"xhs_{aweme_id}_{safe}.mp4")

    if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
        log(f"  ✅ 已存在，跳过下载: {out_file}")
    elif not links:
        log("  ⚠️ 没有下载地址（可能是图文笔记）")
        return None
    else:
        log(f"  下载中（{len(links)} 个候选地址）...")
        t0 = time.time()
        ok = False
        for i, link in enumerate(links[:3], 1):
            try:
                req2 = urllib.request.Request(link, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Referer": "https://www.xiaohongshu.com/",
                })
                with urllib.request.urlopen(req2, timeout=180) as r2, open(out_file, "wb") as f:
                    while True:
                        chunk = r2.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                if os.path.getsize(out_file) > 10000:
                    ok = True
                    log(f"  ✅ 下载完成（第 {i} 个地址）: {os.path.getsize(out_file):,} 字节，"
                        f"耗时 {time.time()-t0:.1f} 秒")
                    break
            except Exception as e:
                log(f"     地址 {i} 失败: {type(e).__name__}")
        if not ok:
            log("  ❌ 所有地址下载失败")
            return None

    # 补充统计到描述，方便知识库检索
    stat_txt = "  ".join(f"{k} {v}" for k, v in stats.items() if v is not None)
    full_desc = f"{desc}\n\n互动数据: {stat_txt}\n标签: {d.get('作品标签') or '无'}\n发布时间: {pub}"

    # 小红书 API 不给时长，用 ffprobe 补
    dur = probe_duration(out_file)
    if dur:
        log(f"  实际时长: {dur:.1f} 秒（ffprobe）")

    return {"file": out_file, "title": title, "desc": full_desc, "platform": "xiaohongshu",
            "aweme_id": aweme_id, "duration": dur}


# ==================== 1. 下载 ====================
def download_video(url):
    """按链接自动分派：小红书 → XHS-Downloader；B站 → dash 分离流；其余 → Douyin/TikTok API"""
    u = url.lower()
    if "xiaohongshu" in u or "xhslink" in u:
        return download_xhs(url)
    if "bilibili" in u or "b23.tv" in u:
        return download_bilibili(url)

    hr("STEP 1: 下载视频")
    log(f"  链接: {url}")

    # 1a. 拿元数据
    # 注意：minimal=true 会剥掉 duration 等字段，这里必须用完整响应
    meta_url = f"{DOUYIN_API}/api/hybrid/video_data?" + urllib.parse.urlencode({"url": url, "minimal": "false"})
    log(f"  解析元数据...")
    try:
        with urllib.request.urlopen(meta_url, timeout=60) as r:
            meta = json.loads(r.read().decode())
    except Exception as e:
        log(f"  ❌ 元数据解析失败: {e}")
        return None

    if meta.get("code") != 200:
        log(f"  ❌ API 返回: {json.dumps(meta, ensure_ascii=False)[:300]}")
        return None

    d = meta["data"]
    desc = d.get("desc") or ""
    aweme_id = d.get("aweme_id") or "unknown"

    # ⚠️ platform 字段只在 minimal=true 时出现；minimal=false 要靠 URL 判断
    platform = d.get("platform")
    if not platform:
        u = url.lower()
        if "douyin" in u:
            platform = "douyin"
        elif "bilibili" in u or "b23.tv" in u:
            platform = "bilibili"
        elif "xiaohongshu" in u or "xhslink" in u:
            platform = "xiaohongshu"
        elif "tiktok" in u:
            platform = "tiktok"
        else:
            # 从返回结构反推：有 aweme_id 就是抖音系
            platform = "douyin" if aweme_id != "unknown" else "unknown"

    # duration 在顶层或 video.duration（抖音返回毫秒）
    duration_ms = d.get("duration")
    if not duration_ms:
        v = d.get("video") or {}
        duration_ms = v.get("duration")
    if not duration_ms:
        v = d.get("video") or {}
        br = v.get("bit_rate") or []
        if br:
            duration_ms = (br[0].get("play_addr") or {}).get("duration") or 0
    duration_ms = duration_ms or 0

    # 标题取 desc 的第一段（去掉话题标签）
    title = re.split(r"[#\n]", desc)[0].strip() or f"{platform}-{aweme_id}"
    title = title[:60]

    log(f"  平台    : {platform}")
    log(f"  ID      : {aweme_id}")
    log(f"  时长    : {duration_ms/1000:.1f} 秒")
    log(f"  标题    : {title}")
    log(f"  完整描述: {desc[:100]}")

    # 1b. 下载视频
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:50]
    out_file = os.path.join(WORK_DIR, f"{platform}_{aweme_id}_{safe}.mp4")
    if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
        log(f"  ✅ 已存在，跳过下载: {out_file}")
    else:
        dl_url = f"{DOUYIN_API}/api/download?" + urllib.parse.urlencode({"url": url, "prefix": "true", "with_watermark": "false"})
        log(f"  下载中...")
        t0 = time.time()
        try:
            urllib.request.urlretrieve(dl_url, out_file)
        except Exception as e:
            log(f"  ❌ 下载失败: {e}")
            return None
        log(f"  ✅ 下载完成: {os.path.getsize(out_file):,} 字节，耗时 {time.time()-t0:.1f} 秒")

    return {"file": out_file, "title": title, "desc": desc, "platform": platform,
            "aweme_id": aweme_id, "duration": duration_ms / 1000}


# ==================== 2. 转写 ====================
def transcribe(media, model_size="small"):
    """用 faster-whisper 转写（ffmpeg 抽音频，绕过 PyAV bug）"""
    hr("STEP 2: 转写")

    wav = os.path.join(WORK_DIR, "_tmp_audio.wav")
    cmd = [FFMPEG, "-y", "-i", media, "-vn", "-ac", "1", "-ar", "16000",
           "-acodec", "pcm_s16le", wav]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log(f"  ❌ ffmpeg 失败: {r.stderr[-400:]}")
        return None

    import numpy as np
    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    log(f"  音频: {n/sr:.1f} 秒 @ {sr}Hz")

    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=8)
    t_load = time.time() - t0

    t0 = time.time()
    segments, info = model.transcribe(audio, language="zh", beam_size=5,
                                      vad_filter=True,
                                      vad_parameters=dict(min_silence_duration_ms=500))
    segs = list(segments)
    t_tr = time.time() - t0

    full = "".join(s.text.strip() for s in segs)
    try:
        import opencc
        full = opencc.OpenCC("t2s").convert(full)
    except Exception:
        pass

    ratio = info.duration / t_tr if t_tr else 0
    log(f"  模型: {model_size}  加载 {t_load:.1f}s  转写 {t_tr:.1f}s")
    log(f"  ⭐ 实时倍率: {ratio:.2f}x   （1 小时音频约 {3600/ratio/60:.1f} 分钟）")
    log(f"  片段 {len(segs)} 个 / {len(full)} 字")

    if not full.strip():
        log()
        log("  ⚠️ 转写结果为空 —— 这个视频可能：")
        log("     ① 没有人声（纯音乐/画面/环境音）")
        log("     ② 音频轨是静音")
        log("     ③ 语音过于微弱")
        log("     → 后续会退化为「只用标题+描述建知识库」")
    log()
    log("  --- 转写稿 ---")
    for s in segs:
        log(f"  [{s.start:6.2f} -> {s.end:6.2f}]  {s.text.strip()}")

    # 带时间戳的完整稿
    with_ts = "\n".join(f"[{s.start:.2f}-{s.end:.2f}] {s.text.strip()}" for s in segs)

    try:
        os.remove(wav)
    except Exception:
        pass

    return {"text": full, "with_timestamp": with_ts, "segments": len(segs),
            "duration": info.duration, "ratio": ratio}


# ==================== 3. 写入 Dify ====================
def dify_cli(args):
    r = subprocess.run([NODE, "bin/difywf.js"] + args,
                       cwd=DIFY_MCP_DIR, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "raw": r.stdout[:400], "err": r.stderr[:300]}


def correct_transcript(info, tr):
    """
    STEP 2.5: 调 Dify 工作流「转写稿纠错与知识点提取」

    ASR 会出错别字（工作榴→工作流、详情业→详情页、时操→实操），
    这一步用 LLM 纠错并提取知识点 + 问答对。

    工作流结构：开始 -> LLM(thinking:false) -> 剥离思维链 -> 结束
    """
    hr("STEP 2.5: 纠错 + 知识点提取（Dify 工作流）")
    log(f"  工作流: {CORRECTION_APP_ID}")

    INPUTS = json.dumps({
        "title": info["title"],
        "desc": info["desc"],
        "transcript": tr["text"],
    }, ensure_ascii=False)

    # 用 Node 脚本调用（避免 PowerShell 传参破坏 JSON 中的中文/引号）
    runner = os.path.join(DIFY_MCP_DIR, "_run-correction.mjs")
    if not os.path.exists(runner):
        log(f"  ❌ 找不到调用脚本: {runner}")
        return None

    t0 = time.time()
    r = subprocess.run([NODE, runner, CORRECTION_APP_ID, INPUTS],
                       cwd=DIFY_MCP_DIR, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    elapsed = time.time() - t0

    out = (r.stdout or "").strip()
    try:
        res = json.loads(out)
    except Exception:
        log(f"  ❌ 返回非 JSON: {out[:300]}")
        if r.stderr:
            log(f"     stderr: {r.stderr[:200]}")
        return None

    if not res.get("ok"):
        log(f"  ❌ 工作流失败: {json.dumps(res, ensure_ascii=False)[:400]}")
        return None

    clean = res.get("clean") or ""
    log(f"  ✅ 完成，耗时 {elapsed:.1f} 秒")
    log(f"  clean_len={res.get('clean_len')}  removed_chars={res.get('removed_chars')}  has_think={res.get('has_think')}")
    log()
    log("  --- 纠错后（前 400 字）---")
    for line in clean[:400].split("\n"):
        log(f"  {line}")
    return clean


# 自定义分段的块间分隔符（配合 process_rule.mode="custom"）
SEG_SEP = "\n\n===\n\n"
SEG_MAX_CHARS = 900      # 单块上限（中文约 1.5 字/token → 900 字 ≈ 600 token）


def build_chunks(info, corrected):
    """
    把纠错稿切成「块」，每块都以【视频标题】开头。

    ⚠️ 为什么要这样做（实测教训）：
       Dify 自动分段会把长视频切成 12+ 段，每段只有 400-500 字，
       段落本身不带任何来源标识 → 检索时多个视频的片段互相串台。
       实测：「电商详情页生成工作流」这个查询命中了长视频里讲「调色台」的片段（0.786）。

    加标题前缀后：
       ① 每段的向量里都含标题关键词，跨视频区分度提高
       ② LLM 拿到的片段自带来源，标注来源更准
    """
    prefix = f"【{info['title']}】\n"

    head = (f"{prefix}"
            f"平台: {info['platform']} / {info['aweme_id']}\n"
            f"时长: {info['duration']:.1f} 秒\n"
            f"作者描述: {(info['desc'] or '')[:300]}")

    chunks = [head]

    # 按 ## 分节（纠错稿结构：## 校对稿 / ## 知识点 / ## 问答对）
    parts = re.split(r"(?m)^(?=##\s)", corrected or "")
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if len(part) <= SEG_MAX_CHARS:
            chunks.append(prefix + part)
            continue
        # 太长就按段落聚合成 <=SEG_MAX_CHARS 的块
        cur = ""
        for para in part.split("\n"):
            if cur and len(cur) + len(para) + 1 > SEG_MAX_CHARS:
                chunks.append(prefix + cur.strip())
                cur = para + "\n"
            else:
                cur += para + "\n"
        if cur.strip():
            chunks.append(prefix + cur.strip())
    return chunks


def write_to_dify(info, tr, dataset_id, corrected=None):
    hr("STEP 3: 写入 Dify 知识库")
    log(f"  知识库: {dataset_id}")

    # ⚠️ 两个实测教训都体现在这里：
    #   ① 不写原始转写稿（带错字的段落会参与检索，污染命中）
    #   ② 用自定义分段，每块带【标题】前缀（提高跨视频区分度）
    if corrected:
        log("  使用纠错稿作为唯一内容（不写原始稿，避免错字段落污染检索）")
        chunks = build_chunks(info, corrected)
        content = SEG_SEP.join(chunks)
        log(f"  ⭐ 切成 {len(chunks)} 块，每块带【{info['title'][:20]}】前缀")
        log(f"     块大小: {[len(c) for c in chunks]}")
    else:
        log("  ⚠️ 没有纠错稿，只能用原始转写稿（会有错字）")
        head = (f"【{info['title']}】\n平台: {info['platform']} / {info['aweme_id']}\n"
                f"时长: {info['duration']:.1f} 秒\n作者描述: {(info['desc'] or '')[:300]}")
        content = SEG_SEP.join([head, f"【{info['title']}】\n{tr['with_timestamp']}",
                                f"【{info['title']}】\n{tr['text']}"])

    doc_name = f"{info['platform']}-{info['title'][:40]}-{info['aweme_id']}"
    doc_name = re.sub(r'[\\/:*?"<>|]', "_", doc_name)

    # 3a. 上传文件
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    file_meta = json.dumps({"name": f"{doc_name}.txt", "content_b64": b64, "mime": "text/plain"}, ensure_ascii=False)
    up = dify_cli(["file", "upload", "--file", file_meta, "--base-url", DIFY_BASE])
    if not up.get("ok"):
        log(f"  ❌ 上传失败: {json.dumps(up, ensure_ascii=False)[:400]}")
        return None
    file_id = up["data"]["id"]
    log(f"  ✅ 文件已上传: {file_id}")

    # 3b. 创建文档
    # ⭐ 用桥接脚本传完整 body：create_document 的 --file-ids 快捷方式会强制
    #    process_rule.mode="automatic"，拿不到自定义分隔符。
    payload_path = os.path.join(DIFY_MCP_DIR, "_add-doc-payload.json")
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({
            "dataset_id": dataset_id,
            "name": doc_name,
            "file_id": file_id,
            "separator": SEG_SEP,
            "max_tokens": 1400,
        }, f, ensure_ascii=False)

    bridge = os.path.join(DIFY_MCP_DIR, "_add-document.mjs")
    if not os.path.exists(bridge):
        log(f"  ❌ 找不到桥接脚本: {bridge}")
        return None
    r = subprocess.run([NODE, bridge, payload_path], cwd=DIFY_MCP_DIR,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    try:
        create = json.loads((r.stdout or "").strip())
    except Exception:
        log(f"  ❌ 桥接返回非 JSON: {(r.stdout or '')[:300]}")
        return None
    if not create.get("ok"):
        log(f"  ❌ 创建文档失败: {json.dumps(create, ensure_ascii=False)[:400]}")
        return None
    doc_id = create["doc_id"]
    log(f"  ✅ 文档已创建: {doc_id}  (自定义分段，sep={SEG_SEP!r})")

    # 3c. 等索引
    log("  等索引完成...")
    for i in range(12):
        time.sleep(5)
        docs = dify_cli(["knowledge", "list-documents", "--dataset-id", dataset_id, "--base-url", DIFY_BASE])
        items = (docs.get("data") or {}).get("data") or []
        status = ", ".join(f"{d['name'][:20]}:{d['indexing_status']}" for d in items)
        log(f"    [{(i+1)*5}s] {status}")
        if items and all(d.get("indexing_status") == "completed" for d in items):
            log("  ✅ 索引完成")
            break

    return {"doc_id": doc_id, "doc_name": doc_name}


# ==================== 4. 检索验证 ====================
def verify(dataset_id, title):
    hr("STEP 4: 检索验证")
    queries = [
        title[:10] if title else "视频内容",
        "这个视频讲了什么",
        "主要内容",
    ]
    for q in queries:
        hit = dify_cli(["knowledge", "hit-testing", "--dataset-id", dataset_id,
                        "--body", json.dumps({"query": q}, ensure_ascii=False),
                        "--base-url", DIFY_BASE])
        recs = (hit.get("data") or {}).get("records") or []
        log(f"  「{q}」-> {len(recs)} 条")
        for r in recs[:2]:
            score = r.get("score") or 0
            txt = str((r.get("segment") or {}).get("content") or "")[:60].replace("\n", " ")
            log(f"      {score:.3f}  {txt}")


# ==================== main ====================
def main():
    ap = argparse.ArgumentParser(description="视频链接 -> 下载 -> 转写 -> Dify 知识库")
    ap.add_argument("url", help="抖音/B站 视频链接")
    ap.add_argument("--dataset-id", default=DEFAULT_DATASET, help="目标知识库 ID")
    ap.add_argument("--model", default="small", help="whisper 模型: tiny/base/small/medium")
    ap.add_argument("--skip-dify", action="store_true", help="只下载+转写，不写知识库")
    ap.add_argument("--skip-correct", action="store_true", help="跳过 LLM 纠错，直接用原始转写稿")
    args = ap.parse_args()

    t_start = time.time()
    hr()
    hr("一键链路：视频 -> 知识库")
    hr()
    log(f"  链接   : {args.url}")
    log(f"  知识库 : {args.dataset_id}")
    log(f"  模型   : {args.model}")
    log(f"  开始   : {time.strftime('%H:%M:%S')}")
    log()

    # 1. 下载
    info = download_video(args.url)
    if not info:
        log("\n❌ 下载失败，终止")
        sys.exit(1)

    # 2. 转写
    tr = transcribe(info["file"], args.model)
    if not tr:
        log("\n❌ 转写失败，终止")
        sys.exit(1)

    # 2.5 纠错 + 知识点提取（Dify 工作流）
    corrected = None
    if args.skip_correct:
        log("\n(按 --skip-correct 跳过纠错)")
    else:
        corrected = correct_transcript(info, tr)
        if not corrected:
            log("\n⚠️ 纠错失败，将使用原始转写稿")
            corrected = None

    # 3. 写知识库
    if args.skip_dify:
        log("\n(按 --skip-dify 跳过知识库写入)")
        result = None
    else:
        result = write_to_dify(info, tr, args.dataset_id, corrected)
        if result:
            verify(args.dataset_id, info["title"])
        else:
            log("\n⚠️ 知识库写入失败（视频和转写稿已保留）")

    # 汇总
    hr()
    hr("完成")
    hr()
    log(f"  视频文件 : {info['file']}")
    log(f"  视频时长 : {info['duration']:.1f} 秒")
    log(f"  转写字数 : {len(tr['text'])} 字")
    log(f"  实时倍率 : {tr['ratio']:.2f}x")
    if corrected:
        log(f"  纠错稿   : {len(corrected)} 字")
    if result:
        log(f"  知识库文档: {result['doc_name']} ({result['doc_id']})")
    log(f"  总耗时   : {time.time()-t_start:.1f} 秒")
    log()
    if corrected:
        log("  纠错稿预览:")
        log(f"    {corrected[:250]}")
    else:
        log("  转写稿预览（未纠错）:")
        log(f"    {tr['text'][:200]}")


if __name__ == "__main__":
    main()
