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
import difflib
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

# ⚠️ 2026-10-08 改成**幂等**：原来无条件包一层 sys.stdout，
#    导致本模块**一个进程里只能 import 一次** ——
#    第二次 import 时 sys.stdout.buffer 已是被包过的、底层 buffer 已关闭，报
#    `ValueError: I/O operation on closed file`。
#    实测踩到：三个测试模块一起跑（各自 importlib 载入本模块）直接崩。
if not getattr(sys, "_dsh_pipeline_stdout_wrapped", False):
    try:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys._dsh_pipeline_stdout_wrapped = True
    except Exception:
        pass

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

# 入知识库时保留哪几节（纠错稿结构：## 校对稿 / ## 知识点 / ## 问答对）
# ⚠️ 默认丢掉「校对稿」——那是他人视频的完整原文，存进公司知识库法律风险最高。
#    只留「知识点 + 问答对」= 提炼产物，属于「思想」而非「表达」。
#    要回退成原有行为，设环境变量 KEEP_SECTIONS=校对稿,知识点,问答对
_ks = os.environ.get("KEEP_SECTIONS", "").strip()
DEFAULT_KEEP_SECTIONS = tuple(s.strip() for s in _ks.split(",") if s.strip()) \
    if _ks else ("知识点", "问答对")

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


# ==================== 1a-2. B站分P 下载（2026-10-08 新增）====================
# ⚠️ 事故风险：原 download_bilibili() 取 fetch_one_video 的**顶层 cid（= P1）**，
#    只下这一路 dash 流 → 多分P 课程只入库 P1，其余**静默丢失且不报错**。
#    实测 BV1mhJM6XESc：19 分P / 总 124.5 分钟 → 旧逻辑丢 113 分钟。
#    分P 信息在 d["pages"]（每项含 page/cid/part/duration）。
# 参考：拾语 shiyu-ai-knowledge-base「多分P系列一键排队批量总结并合并合集笔记」。

def part_filename(bv, page, part):
    """分P 的落盘文件名。多P 必须在文件名里带 P 号，否则后一个覆盖前一个。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", part or "")[:40]
    return os.path.join(WORK_DIR, f"bilibili_{bv}_P{page}_{safe}.mp4")


# ---------------- 按内容去重（音频指纹，2026-10-08 新增）----------------
# 背景：BV1mhJM6XESc 的 **P2/P3 是同一节课的重复投稿** ——
#   标题一样、时长一样（363 秒）、**音频指纹完全相同**（视频重编码过，所以文件不同）。
#   按分P 无脑入库 → 库里多一篇几乎一样的文档，检索时互相抢位。
# 做法：下载后先算音频前 N 秒的 md5，命中已入库指纹就跳过 →
#   **「按内容分」而不是「按链接分」**。
FP_DB = os.path.join(WORK_DIR, "_ingested-fingerprints.json")
FP_SECONDS = 120


def audio_fingerprint(media, seconds=FP_SECONDS):
    """音频指纹 = 抽前 N 秒 → 16k 单声道 wav → md5。算不出返回 None。"""
    if not media or not os.path.exists(media):
        return None
    import hashlib
    wav = os.path.join(WORK_DIR, "_tmp_fingerprint.wav")
    cmd = [FFMPEG, "-y", "-v", "error", "-t", str(seconds), "-i", media,
           "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", wav]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except Exception:
        return None
    if r.returncode != 0 or not os.path.exists(wav):
        return None
    h = hashlib.md5()
    try:
        with open(wav, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass
    return h.hexdigest()


def load_fp_db(db_path=None):
    """读指纹台账。文件不存在/损坏都返回空 dict（**绝不因此崩掉整条链路**）"""
    p = db_path or FP_DB
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def fp_lookup(fp, db_path=None):
    """这个指纹入库过吗？入库过返回当时的记录（含 title/doc_id），否则 None"""
    if not fp:
        return None
    return load_fp_db(db_path).get(fp)


def fp_record(fp, info, doc_id, db_path=None):
    """登记一个已入库内容的指纹"""
    if not fp:
        return
    p = db_path or FP_DB
    db = load_fp_db(p)
    db[fp] = {"title": (info or {}).get("title", ""), "doc_id": doc_id,
              "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  ⚠️ 指纹台账写入失败（不影响本次入库）: {e}")


def build_part_titles(video_title, pages):
    """把 pages 规格化成 [{page, cid, title, part}]。

    ⭐ 纯函数（不碰网络），所以能单测。
    · 单P → 标题不加 P 前缀（否则库里所有单P视频名字都变）
    · 多P → 标题带 P 号 + 分P 名，且**保证唯一**（同名文档会互相混淆）
    """
    pages = [p for p in (pages or []) if p]
    if not pages:
        return [{"page": 1, "cid": None, "title": (video_title or "")[:60], "part": ""}]
    multi = len(pages) > 1
    out, seen = [], set()
    for p in pages:
        page = int(p.get("page") or 1)
        part = (p.get("part") or "").strip()
        t = (f"{video_title} P{page} {part}".strip() if multi else (video_title or ""))[:60]
        base, k = t, 2
        while t in seen:                       # 去重
            t = f"{base} ({k})"[:60]
            k += 1
        seen.add(t)
        out.append({"page": page, "cid": p.get("cid"), "title": t, "part": part})
    return out


def _bili_streams(bv, cid):
    """取某个 cid 的 dash 视频流/音频流（各选一条）"""
    play_res = _bili_get("/api/bilibili/web/fetch_video_playurl", {"bv_id": bv, "cid": cid})
    pd = (play_res.get("data") or {}).get("data") or {}
    dash = pd.get("dash") or {}
    vids, auds = dash.get("video") or [], dash.get("audio") or []

    def pick(arr, prefer_avc=True):
        if not arr:
            return None
        if prefer_avc:
            avc = [x for x in arr if str(x.get("codecs", "")).startswith("avc")]
            if avc:
                return max(avc, key=lambda x: x.get("bandwidth") or 0)
        return max(arr, key=lambda x: x.get("bandwidth") or 0)

    return pick(vids), pick(auds, prefer_avc=False)


def _bili_download_one(bv, cid, out_file, tag=""):
    """下载并合并一个 cid 的 dash 流。成功返回 out_file，失败 None。"""
    if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
        log(f"  ✅ 已存在，跳过下载: {os.path.basename(out_file)}")
        return out_file
    v, a = _bili_streams(bv, cid)
    if not v:
        log(f"  ❌ {tag} dash 里没有视频流")
        return None
    vtmp, atmp = out_file + ".v.m4s", out_file + ".a.m4s"
    try:
        sz_v = _fetch_file(v["baseUrl"], vtmp)
        sz_a = _fetch_file(a["baseUrl"], atmp) if a else 0
    except Exception as e:
        log(f"  ❌ {tag} 流下载失败: {type(e).__name__} {e}")
        return None
    cmd = ([FFMPEG, "-y", "-i", vtmp, "-i", atmp, "-c", "copy", out_file]
           if a and sz_a > 0 else [FFMPEG, "-y", "-i", vtmp, "-c", "copy", out_file])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for f in (vtmp, atmp):
        try:
            os.remove(f)
        except Exception:
            pass
    if r.returncode != 0 or not os.path.exists(out_file):
        log(f"  ❌ {tag} 合并失败: {r.stderr[-300:]}")
        return None
    log(f"    {tag} 已下 {os.path.getsize(out_file):,} 字节")
    return out_file


def download_bilibili_parts(url):
    """B站下载（**支持分P**）→ 返回 info **列表**，每个分P 一项"""
    hr("STEP 1: 下载视频（B站）")
    log(f"  链接: {url}")
    if "b23.tv" in url:
        url = _resolve_short_link(url)
        log(f"  短链已解析: {url[:100]}")

    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        log("  ❌ 从链接里提不到 BV 号")
        return []
    bv = m.group(1)

    try:
        info_res = _bili_get("/api/bilibili/web/fetch_one_video", {"bv_id": bv})
    except Exception as e:
        log(f"  ❌ 取视频信息失败: {e}")
        return []
    d = (info_res.get("data") or {}).get("data") or {}
    if not d:
        log(f"  ❌ 返回无 data: {json.dumps(info_res, ensure_ascii=False)[:250]}")
        return []

    title = (d.get("title") or "").strip()[:60]
    desc = (d.get("desc") or "").strip()
    cid0 = d.get("cid")
    dur_ms = d.get("duration") or 0
    owner, stat = d.get("owner") or {}, d.get("stat") or {}
    log(f"  平台    : bilibili   BV 号: {bv}")
    log(f"  标题    : {title}")
    log(f"  UP 主   : {owner.get('name')} ({owner.get('mid')})")
    log(f"  时长    : {dur_ms} 秒")

    pages = d.get("pages") or []
    if not pages and cid0:
        pages = [{"page": 1, "cid": cid0, "part": title, "duration": dur_ms}]
    specs = build_part_titles(title, pages)
    if len(specs) > 1:
        log(f"  ⭐ 这是 **{len(specs)} 分P** 视频 → 逐P 下载，各自成一个文档")
    else:
        log("  单P 视频")

    full_desc = (f"{desc}\n\nUP主: {owner.get('name')}\n"
                 f"互动数据: 播放 {stat.get('view')}  点赞 {stat.get('like')}  "
                 f"收藏 {stat.get('favorite')}  评论 {stat.get('reply')}  投币 {stat.get('coin')}")

    infos = []
    for i, sp in enumerate(specs, 1):
        pg, cid_i, t, part = sp["page"], sp["cid"], sp["title"], sp["part"]
        tag = f"[{i}/{len(specs)}] P{pg}" if len(specs) > 1 else "  "
        if not cid_i:
            log(f"  ⚠️ {tag} 没有 cid，跳过")
            continue
        if len(specs) > 1:
            out_file = part_filename(bv, pg, part)
        else:
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:50]
            out_file = os.path.join(WORK_DIR, f"bilibili_{bv}_{safe}.mp4")
        log(f"  {tag} {t[:46]}")
        got = _bili_download_one(bv, cid_i, out_file, tag=tag)
        if not got:
            continue
        dur = probe_duration(got) or 0
        infos.append({"file": got, "title": t, "desc": full_desc,
                      "platform": "bilibili", "aweme_id": bv, "duration": dur,
                      "page": pg, "pages_total": len(specs)})
    log(f"  ✅ 分P 下载完成：成功 {len(infos)}/{len(specs)}")
    return infos


def download_any_parts(url):
    """统一入口：返回 info **列表**（B站可能多P；其它平台永远一项）

    ⚠️ 别直接用 download_video()：B站多分P 会被它静默截成 P1。
    """
    u = (url or "").lower()
    if "bilibili" in u or "b23.tv" in u:
        return download_bilibili_parts(url)
    one = download_video(url)
    return [one] if one else []


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
    # 2026-10-08：抽音频时顺带降噪（highpass/lowpass/loudnorm），
    # 依据见 ffmpeg_audio_args 的注释
    cmd = ffmpeg_audio_args(media, wav)
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


# ==================== 纠错链路工具（2026-10-08 新增）====================
# 事故背景：64 分钟视频（转写 19093 字）一次性喂给纠错 LLM →
#   「校对稿」一节吃光 max_tokens，`## 知识点`/`## 问答对` 被截断 →
#   合规策略正确丢掉校对稿 → 入库只剩 105 字，而流程报「✅ 完成」。
#
# 参考实现（开源调研）：
#   · 拾语 shiyu-ai-knowledge-base backend/cleaner.py
#       「分块 map-reduce…避免整篇一次性总结丢信息、跑题」
#       `_split_text_at(text, 3500)`：按 3500 字、句子边界切
#   · learning-video-skills：闸门要「真检查非存在性检查」

# 分块阈值：抄拾语的 3500 字。撞的是**输出 token 上限**，所以按字数切；
# 3500 字 ≈ 5300 token，配合纠错节点 max_tokens=8000 有充足余量。
CHUNK_CHARS = 3500

# 句末标点。用零宽后顾切分（不消费字符）→ 保证 ''.join(块) == 原文
SENT_END = r"(?<=[。！？!?；;])"

# 规则清洗要去的口头语。⚠️ 只在**句首**去 —— 句子中间的「然后呢」是正常表达
FILLERS = ("嗯", "呃", "唉", "哦", "额", "那个", "就是说")


def ffmpeg_audio_args(src, dst):
    """抽音频的 ffmpeg 参数（含降噪）

    降噪依据：阿里云开发者社区《AI 驱动的视频课程自动摘要与知识点提取》——
      highpass 去低频噪音、lowpass 去高频噪音、loudnorm 做 EBU R128 响度归一化
    """
    return [FFMPEG, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
            "-af", "highpass=f=80,lowpass=f=8000,loudnorm=I=-16:TP=-1.5",
            "-acodec", "pcm_s16le", dst]


def rule_clean(text):
    """规则清洗：进 LLM 之前先做一遍机械清洗（拾语的做法）"""
    if not text:
        return ""
    t = text
    for f in FILLERS:
        t = re.sub(rf"(?m)(?<=[。！？；\n])\s*{re.escape(f)}[，,、]?\s*", "", t)
        t = re.sub(rf"\A\s*{re.escape(f)}[，,、]?\s*", "", t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def split_by_chars(text, limit=CHUNK_CHARS):
    """按句子边界切成 <= limit 的块。

    ⭐ 不变量：''.join(结果) == text —— 一个字都不能丢
    """
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for s in re.split(SENT_END, text):      # 零宽切分
        if not s:
            continue
        if cur and len(cur) + len(s) > limit:
            chunks.append(cur)
            cur = s
        else:
            cur += s
        while len(cur) > limit:             # 单句超限 → 硬切
            chunks.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        chunks.append(cur)
    return chunks


def _section(text, name):
    m = re.search(rf"(?ms)^##\s*{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", text or "")
    return m.group(1).strip() if m else ""


def _dedupe_bullets(blocks):
    seen, out = set(), []
    for b in blocks:
        for ln in (b or "").splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("-"):
                key = re.sub(r"\s+", "", s)
                if key in seen:
                    continue
                seen.add(key)
            out.append(ln.rstrip())
    return "\n".join(out).strip()


def _dedupe_qa(blocks):
    seen, out = set(), []
    for b in blocks:
        for piece in re.split(r"(?m)(?=^Q[:：])", b or ""):
            c = piece.strip()
            if not c:
                continue
            m = re.match(r"^Q[:：]\s*(.+)", c)
            if m:
                key = re.sub(r"\s+", "", m.group(1))[:60]
                if key in seen:
                    continue
                seen.add(key)
            out.append(c)
    return "\n\n".join(out).strip()


def merge_corrected(parts):
    """把 N 块的纠错结果合并成一份（map → reduce），知识点/问答对去重"""
    out = []
    proof = "\n\n".join(x for x in (_section(p, "校对稿") for p in parts) if x)
    if proof:
        out.append("## 校对稿\n" + proof)
    kp = _dedupe_bullets([_section(p, "知识点") for p in parts])
    if kp:
        out.append("## 知识点\n" + kp)
    qa = _dedupe_qa([_section(p, "问答对") for p in parts])
    if qa:
        out.append("## 问答对\n" + qa)
    return "\n\n".join(out)


def check_correction(parts, merged):
    """校验闸门 —— **真检查，不是存在性检查**

    查三件事：
      1. 每一块都必须出「知识点」或「问答对」—— 缺了说明该块输出被截断
      2. 整体必须至少有一节
      3. 「知识点」里要有**实际条目**（只查标题存在 = 存在性检查，不算）

    返回 (ok, reasons)
    """
    reasons = []
    SECTIONS = ("知识点", "问答对")
    for i, p in enumerate(parts or [], 1):
        if not any(f"## {s}" in (p or "") for s in SECTIONS):
            reasons.append(f"第 {i} 块被截断：既没有「知识点」也没有「问答对」小节（输出可能撞了 max_tokens）")
    if not any(f"## {s}" in (merged or "") for s in SECTIONS):
        reasons.append("整体没有任何「知识点」或「问答对」小节")
    else:
        kp = _section(merged, "知识点")
        items = [l for l in kp.splitlines()
                 if l.strip().startswith("-") and len(l.strip()) >= 6]
        if not items:
            qa = _section(merged, "问答对")
            if len(re.findall(r"(?m)^Q[:：]", qa)) == 0:
                reasons.append("既没有有效的「知识点」条目，也没有有效的「问答对」")
    return (len(reasons) == 0, reasons)


# ---------------- 专名漂移检查（2026-10-08 新增，治 Act→Actor）----------------
# 依据：learning-video-skills「三个不会报错但会毁掉产出的坑」之一：
#   「专有名词以 **OCR** 为准 —— 声学模型解决不了同音词（「短距」/「短剧」），
#     画面文字才是可靠来源。」
# 本机没接 OCR，退一步做可测的替代：把「静默改写专名」变成**可见告警**。

TERM_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "that", "this", "have", "has",
    "are", "was", "were", "will", "can", "not", "but", "from", "they", "them",
    "his", "her", "its", "our", "out", "use", "used", "using", "one", "two",
    "all", "any", "how", "what", "when", "why", "who", "which", "there", "here",
    "about", "into", "over", "just", "like", "also", "then", "than", "some",
    "pro", "app", "www", "com", "http", "https",
}
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{2,}")


def extract_terms(text):
    """抽候选专名（ASCII/技术词）：去虚词、去重、保序"""
    out, seen = [], set()
    for m in _TERM_RE.findall(text or ""):
        t = m.strip(".-")
        if len(t) < 3 or t.lower() in TERM_STOPWORDS:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _term_present(term, text):
    """专名是否**作为独立词**出现在文本里。

    ⚠️ 不能简单用 `term in text` —— 实测踩到：
       "Act" 是 "Actor" 的子串 → 子串匹配会认为 Act 还在，于是 Act→Actor 漏报。
       必须加字母/数字边界。
    """
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                     text or "", re.IGNORECASE) is not None


def find_term_drift(raw, corrected, min_ratio=0.7):
    """找「原稿里有、纠错稿里没了」的专名，并猜它被改成了什么

    ⭐ Act→Actor 实测：SequenceMatcher('act','actor') = 0.75 ≥ 0.7 → 抓到
    """
    if not raw:
        return []
    corr_terms = extract_terms(corrected)
    out = []
    for t in extract_terms(raw):
        if _term_present(t, corrected):         # 作为独立词还在 → 没漂移
            continue
        # ⭐ 猜「被改成了什么」时，优先挑**原稿里没有的**新词。
        #    实测踩到：ReAct 和 Actor 与 Act 的相似度都是 0.75，
        #    不筛新词就会误报成「Act → ReAct」——而 ReAct 原稿本来就有，不可能是它。
        fresh = [c for c in corr_terms if not _term_present(c, raw)]
        pool = fresh or corr_terms
        suspect, best = None, 0.0
        for c in pool:
            r = difflib.SequenceMatcher(None, t.lower(), c.lower()).ratio()
            if r > best:
                best, suspect = r, c
        if best < min_ratio:
            suspect = None
        out.append({"term": t, "suspect": suspect, "ratio": round(best, 3)})
    return out


def format_drift_report(drifts):
    """把漂移列表变成给人看的告警文本；无漂移返回空串"""
    if not drifts:
        return ""
    lines = ["专名漂移告警（原稿有、纠错稿里没了）："]
    for d in drifts:
        if d.get("suspect"):
            lines.append(f"  · {d['term']} → 疑似被改成 {d['suspect']}（相似度 {d['ratio']}）")
        else:
            lines.append(f"  · {d['term']} 直接消失（没找到相似词）")
    lines.append("  ⚠️ 专名不信任 LLM 纠错：请对照视频画面文字（OCR）核对")
    return "\n".join(lines)


def _call_correction_once(info, text, timeout=300):
    """调一次纠错工作流，返回 clean 文本或 None"""
    INPUTS = json.dumps({"title": info["title"], "desc": info["desc"],
                         "transcript": text}, ensure_ascii=False)
    runner = os.path.join(DIFY_MCP_DIR, "_run-correction.mjs")
    if not os.path.exists(runner):
        log(f"  ❌ 找不到调用脚本: {runner}")
        return None
    r = subprocess.run([NODE, runner, CORRECTION_APP_ID, INPUTS],
                       cwd=DIFY_MCP_DIR, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
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
    return res.get("clean") or ""


def correct_transcript(info, tr):
    """
    STEP 2.5: 纠错 + 知识点提取（Dify 工作流 · **分块 map-reduce**）

    ⚠️ 2026-10-08 改造：不再把整篇一次性喂进去。
       长转写稿会让「校对稿」一节吃光 max_tokens，后面的知识点/问答对全被截断。
       现在：规则清洗 → 按 3500 字句子边界切块 → 逐块纠错 → 合并去重 → 闸门校验。
    """
    hr("STEP 2.5: 纠错 + 知识点提取（Dify 工作流 · 分块 map-reduce）")
    log(f"  工作流: {CORRECTION_APP_ID}")

    raw = rule_clean(tr.get("text") or "")
    parts_in = split_by_chars(raw, CHUNK_CHARS)
    if not parts_in:
        log("  ❌ 转写稿是空的")
        return None
    log(f"  规则清洗后 {len(raw)} 字 → 切 {len(parts_in)} 块（阈值 {CHUNK_CHARS} 字，句子边界）")

    parts_out, t0 = [], time.time()
    for i, seg in enumerate(parts_in, 1):
        log(f"  [{i}/{len(parts_in)}] 纠错中（{len(seg)} 字）...")
        got = _call_correction_once(info, seg)
        if got is None:
            log(f"  ❌ 第 {i} 块纠错失败，整篇中止（不写半成品）")
            return None
        parts_out.append(got)

    merged = merge_corrected(parts_out)
    ok, reasons = check_correction(parts_out, merged)
    elapsed = time.time() - t0

    if not ok:
        dump = os.path.join(WORK_DIR, f"_failed-correction-{int(time.time())}.txt")
        try:
            with open(dump, "w", encoding="utf-8") as f:
                for i, p in enumerate(parts_out, 1):
                    f.write(f"===== 第 {i} 块原始产出 =====\n{p}\n\n")
        except Exception:
            dump = "(落盘失败)"
        log(f"  ❌ 校验闸门未通过（真检查）—— **不入库**，避免产出近空文档")
        for rr in reasons:
            log(f"     · {rr}")
        log(f"     各块原始产出已落盘留证: {dump}")
        return None

    log(f"  ✅ 通过校验闸门，耗时 {elapsed:.1f} 秒")
    log(f"     合并后 {len(merged)} 字（校对稿/知识点/问答对三节齐全，已去重）")

    # 专名漂移检查（真检查）：把「静默改写专名」变成可见告警
    drifts = find_term_drift(raw, merged)
    if drifts:
        log("")
        for line in format_drift_report(drifts).split("\n"):
            log(f"  {line}")
        try:
            dp = os.path.join(WORK_DIR, f"_term-drift-{int(time.time())}.txt")
            with open(dp, "w", encoding="utf-8") as f:
                f.write(format_drift_report(drifts))
                f.write("\n\n原始转写稿（专名部分上下文）：\n")
                for d in drifts:
                    i = raw.find(d["term"])
                    if i >= 0:
                        f.write(f"  [{d['term']}] …{raw[max(0,i-60):i+80]}…\n")
            log(f"     漂移明细已落盘: {dp}")
        except Exception:
            pass
    else:
        log("     专名漂移检查: ✅ 无漂移")

    log()
    log("  --- 纠错后（前 400 字）---")
    for line in merged[:400].split("\n"):
        log(f"  {line}")
    return merged


# 自定义分段的块间分隔符（配合 process_rule.mode="custom"）
SEG_SEP = "\n\n===\n\n"
SEG_MAX_CHARS = 900      # 单块上限（中文约 1.5 字/token → 900 字 ≈ 600 token）

# ---- 事实摘录（从被丢弃的「校对稿」里，只救回含数值/参数的句子）----
# 为什么可以这样做：
#   著作权法保护「表达」，不保护「事实」。数值、型号、参数名、版本号是事实，
#   不受保护；而它们恰恰是检索时最有用的信息（用户常问「显存多少」「几步」「哪个版本」）。
#   所以丢掉整篇校对稿（表达），但把含事实的句子摘出来留一份。
#
# 三条限制（防止变相保留全文）：
#   ① 只摘含数值或已知参数名的句子，纯观点句不摘
#   ② 总长封顶（FACT_MAX_CHARS），超了就按命中密度截断
#   ③ 明确标注「原文摘录」，不冒充自己的话
FACT_MAX_CHARS = 700

# 数值 + 单位 / 版本号 / 常见参数名
FACT_PATTERNS = [
    r"\d+(?:\.\d+)?\s*(?:G|GB|MB|T|K|k|万|亿|秒|分钟|小时|帧|步|张|个|次|倍|%|％|字|条|段|部|层)",
    r"[Vv]\d+(?:\.\d+)?",
    r"\d+(?:\.\d+)?\s*(?:top_?k|temperature|cfg|steps?|denoise|seed|batch)",
    # 已知的参数 / 型号 / 专有名（可继续追加）
    r"FP16|FP8|BF16|三线性|双线性|罗尔|LoRA|WCA8|底膜|采样器|调色台|柔雾|泛光|暖晕",
    r"ComfyUI|MiniMax|H3|Claude|Codex|Cursor|MCP|SKILL|Skill",
]


def extract_facts(corrected, max_chars=FACT_MAX_CHARS):
    """
    从「校对稿」里摘出含数值 / 参数名的句子，作为「关键参数（原文摘录）」段。

    返回 (文本, 命中句数)。没有校对稿或没摘到就返回 ("", 0)。
    """
    if not corrected:
        return "", 0

    # 定位 ## 校对稿 这一节
    m = re.search(r"(?ms)^##\s*校对稿\s*\n(.*?)(?=^##\s|\Z)", corrected)
    if not m:
        return "", 0
    body = m.group(1)

    pat = re.compile("|".join(f"(?:{p})" for p in FACT_PATTERNS))

    # 按句拆（中文句号/问号/感叹号/分号/换行）
    sentences = re.split(r"(?<=[。！？；!?;])\s*|\n+", body)
    hits = []
    for s in sentences:
        s = s.strip()
        if len(s) < 6 or len(s) > 200:
            continue
        if pat.search(s):
            hits.append(s)

    if not hits:
        return "", 0

    # 去重（保留首次出现）
    seen, uniq = set(), []
    for s in hits:
        k = s[:24]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)

    # 按长度封顶
    out, total = [], 0
    for s in uniq:
        if total + len(s) + 1 > max_chars:
            break
        out.append("- " + s)
        total += len(s) + 3

    if not out:
        return "", 0

    text = ("## 关键参数（原文摘录）\n"
            "以下是从原视频里摘出的含具体数值/参数的句子，供核对事实用：\n"
            + "\n".join(out))
    return text, len(out)


def build_chunks(info, corrected, keep_sections=None):
    """
    把纠错稿切成「块」，每块都以【视频标题】开头。

    ⚠️ 为什么要这样做（实测教训）：
       Dify 自动分段会把长视频切成 12+ 段，每段只有 400-500 字，
       段落本身不带任何来源标识 → 检索时多个视频的片段互相串台。
       实测：「电商详情页生成工作流」这个查询命中了长视频里讲「调色台」的片段（0.786）。

    加标题前缀后：
       ① 每段的向量里都含标题关键词，跨视频区分度提高
       ② LLM 拿到的片段自带来源，标注来源更准

    ⚠️ 为什么默认丢掉「校对稿」（2026-10-07 起）：
       纠错工作流输出三节 —— ## 校对稿 / ## 知识点 / ## 问答对。
       「校对稿」是**他人视频的完整原文**，存进公司知识库属于「复制表达」，
       法律风险最高（著作权法的思想/表达二分法：思想不受保护，表达受保护）。

       默认只留「知识点」+「问答对」：
         · 知识点 = 提炼后的方法论 → 属于「思想」→ 风险低
         · 问答对 = 问题与答案 → 也是提炼产物，且问答形式对检索更友好

       代价：内容总量减少，检索精度可能下降。实测对比见技术文档。

       要回退成「三节全存」，传 keep_sections=("校对稿","知识点","问答对")
    """
    if keep_sections is None:
        keep_sections = DEFAULT_KEEP_SECTIONS

    prefix = f"【{info['title']}】\n"

    # ⚠️ 2026-10-08：不再生成独立的「元数据段」（原为 chunks[0]）。
    #    实测问题：它被 SEG_SEP 切成独立分段后会在检索里抢槽位 ——
    #    「多 Agent 优点」这个查询的前 10 条里，有 3 条是零知识的元数据段（占 30% 槽位）。
    #    而 prefix（【标题】）本来就拼在每个内容块开头 → 那一段几乎全是冗余。
    #    作者描述按用户决定不再保留；平台/时长对检索无价值。
    #    回归测试：test_pipeline_chunks.TestBuildChunks.test_no_standalone_metadata_chunk
    chunks = []

    # 按 ## 分节（纠错稿结构：## 校对稿 / ## 知识点 / ## 问答对）
    parts = re.split(r"(?m)^(?=##\s)", corrected or "")
    dropped = []
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue

        # 判断这一节属于哪一类；不在保留清单里的丢掉
        m = re.match(r"^##\s*(\S+)", part)
        sec = m.group(1) if m else ""
        if sec and sec not in keep_sections:
            dropped.append(f"{sec}({len(part)}字)")
            continue
        if not sec and keep_sections and not any(s in part[:40] for s in keep_sections):
            # 没有 ## 标题的开头段落：只有在保留清单为空时才收
            pass

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

    if dropped:
        log(f"  ⚠️ 按合规策略丢弃: {', '.join(dropped)}")

    # ⭐ 从被丢掉的校对稿里，只救回「含数值/参数」的句子（事实不受著作权保护，
    #    且是检索最有用的信息）。丢掉的「表达」不救。
    if "校对稿" not in keep_sections:
        facts, n_facts = extract_facts(corrected)
        if facts:
            chunks.append(prefix + facts)
            log(f"  ⭐ 从校对稿摘回 {n_facts} 句关键参数（{len(facts)} 字）")
        else:
            log("  · 校对稿里没摘到含数值/参数的句子")

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
        # ⚠️ 2026-10-08：这里同样不再生成元数据段（与 build_chunks 保持一致）
        content = SEG_SEP.join([f"【{info['title']}】\n{tr['with_timestamp']}",
                                f"【{info['title']}】\n{tr['text']}"])

    doc_name = f"{info['platform']}-{info['title'][:40]}-{info['aweme_id']}"
    doc_name = re.sub(r'[\\/:*?"<>|]', "_", doc_name)

    # 3a. 上传文件
    # ⚠️ 2026-10-08 修：不能把 base64 当**命令行参数**传。
    #    9178 字中文 → UTF-8 ≈27KB → base64 ≈36,700 字符 > Windows 命令行上限 32,767
    #    → `FileNotFoundError: [WinError 206] 文件名或扩展名太长`，整篇入不了库。
    #    实测 CLI 的 `--file` 不支持 `@file` 或 `-`(stdin)，只认内联 JSON →
    #    改成：payload 写文件，用桥接脚本 `_upload-file.mjs` 直接调 console API。
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload_path = os.path.join(DIFY_MCP_DIR, "_upload-payload.json")
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({"name": f"{doc_name}.txt", "content_b64": b64, "mime": "text/plain"},
                  f, ensure_ascii=False)
    bridge_up = os.path.join(DIFY_MCP_DIR, "_upload-file.mjs")
    if not os.path.exists(bridge_up):
        log(f"  ❌ 找不到上传桥接脚本: {bridge_up}")
        return None
    ru = subprocess.run([NODE, bridge_up, payload_path], cwd=DIFY_MCP_DIR,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300)
    try:
        up = json.loads((ru.stdout or "").strip())
    except Exception:
        log(f"  ❌ 上传桥接返回非 JSON: {(ru.stdout or '')[:300]}")
        return None
    if not up.get("ok"):
        log(f"  ❌ 上传失败: {json.dumps(up, ensure_ascii=False)[:400]}")
        return None
    file_id = up["data"]["id"]
    log(f"  ✅ 文件已上传: {file_id}  ({up.get('_bytes')} 字节 / base64 {len(b64)} 字符)")

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

    # 1. 下载（B站可能多个分P → 逐P 走完整链路）
    infos = download_any_parts(args.url)
    if not infos:
        log("\n❌ 下载失败，终止")
        sys.exit(1)
    if len(infos) > 1:
        log(f"\n⭐ 共 {len(infos)} 个分P，逐个入库（每个分P 一个文档）")

    ok_cnt, fail_cnt, skip_cnt = 0, 0, 0
    for idx, info in enumerate(infos, 1):
        if len(infos) > 1:
            hr()
            hr(f"分P {idx}/{len(infos)}：{info['title'][:50]}")
            hr()
        try:
            # 1.9 按内容去重：音频指纹已入库过 → 跳过（不转写、不纠错、不入库）
            fp = audio_fingerprint(info["file"])
            if fp:
                dup = fp_lookup(fp)
                if dup:
                    log(f"  ⏭️ 内容重复：音频指纹与已入库的《{dup.get('title', '?')[:40]}》"
                        f"完全相同 → 跳过这一个分P（省掉转写+纠错）")
                    skip_cnt += 1
                    continue

            # 2. 转写
            tr = transcribe(info["file"], args.model)
            if not tr:
                log(f"\n❌ [{info['title'][:30]}] 转写失败，跳过这一P")
                fail_cnt += 1
                continue

            # 2.5 纠错 + 知识点提取（Dify 工作流）
            corrected = None
            if args.skip_correct:
                log("\n(按 --skip-correct 跳过纠错)")
            else:
                corrected = correct_transcript(info, tr)
                if not corrected:
                    # ⚠️ 闸门未通过 = 绝不写半成品（原来会写出 105 字的空文档还报成功）
                    log(f"\n❌ [{info['title'][:30]}] 纠错未通过校验闸门，**跳过这一P**（不写半成品）")
                    fail_cnt += 1
                    continue

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
                    fail_cnt += 1
                    continue

            ok_cnt += 1
            # 登记内容指纹（下次同一个内容直接跳过）
            fp_record(fp, info, (result or {}).get("doc_id"))
            # 单P 时保持原来的详细汇总
            if len(infos) == 1:
                hr(); hr("完成"); hr()
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
                log("  纠错稿预览:")
                log(f"    {(corrected or tr['text'])[:250]}")
        except Exception as e:
            log(f"\n❌ [{info['title'][:30]}] 异常: {type(e).__name__} {e}")
            import traceback
            traceback.print_exc()
            fail_cnt += 1

    if len(infos) > 1:
        hr(); hr("全部完成"); hr()
        log(f"  分P 总数 : {len(infos)}")
        log(f"  成功     : {ok_cnt}")
        log(f"  跳过(重复): {skip_cnt}")
        log(f"  失败     : {fail_cnt}")
        log(f"  总耗时   : {time.time()-t_start:.1f} 秒")
        log()
        log("  ⚠️ 别忘了给新文档绑主题：node _bind-topics.mjs")


if __name__ == "__main__":
    main()
