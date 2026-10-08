# -*- coding: utf-8 -*-
"""
faster-whisper 实测（绕过 PyAV 的 metadata_errors bug）

原理：faster-whisper 的 decode_audio 用 av.open(..., metadata_errors=...)
      而本机 PyAV 19.0.1 不支持这个参数。
      解法：用 ffmpeg 自己抽 16kHz 单声道 WAV，读成 numpy 数组，
            直接传给 model.transcribe()，这样就跳过了 decode_audio。

用法:
    python test-asr2.py [媒体路径] [模型大小]
"""
import os
import sys
import io
import time
import wave
import subprocess

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MEDIA = sys.argv[1] if len(sys.argv) > 1 else r"<你的工作目录>\douyin-test.mp4"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "small"
FFMPEG = r"<你的 ffmpeg 路径>"
WAV = r"<你的工作目录>\audio-16k-mono.wav"

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

print("=" * 72)
print("faster-whisper 实测（ffmpeg 抽音频，绕过 PyAV bug）")
print("=" * 72)
print(f"  媒体     : {MEDIA}")
print(f"  模型     : {MODEL}")
print(f"  ffmpeg   : {FFMPEG}  存在={os.path.exists(FFMPEG)}")
print()

# ---- STEP 1: ffmpeg 抽音频 ----
print("-" * 72)
print("STEP 1: ffmpeg 抽 16kHz 单声道 WAV")
print("-" * 72)
if not os.path.exists(FFMPEG):
    print("  ffmpeg 不存在，退出")
    sys.exit(1)

cmd = [FFMPEG, "-y", "-i", MEDIA, "-vn", "-ac", "1", "-ar", "16000",
       "-acodec", "pcm_s16le", WAV]
t0 = time.time()
r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
t_ff = time.time() - t0
if r.returncode != 0:
    print(f"  ffmpeg 失败: {r.stderr[-600:]}")
    sys.exit(1)
print(f"  OK，耗时 {t_ff:.2f} 秒，输出 {os.path.getsize(WAV):,} 字节")

# ---- STEP 2: 读 WAV 成 numpy ----
print()
print("-" * 72)
print("STEP 2: 读 WAV -> numpy float32")
print("-" * 72)
import numpy as np

with wave.open(WAV, "rb") as w:
    n_ch = w.getnchannels()
    sw = w.getsampwidth()
    sr = w.getframerate()
    n_frames = w.getnframes()
    raw = w.readframes(n_frames)
print(f"  声道={n_ch}  采样宽={sw}字节  采样率={sr}  帧数={n_frames}")
print(f"  音频时长: {n_frames / sr:.2f} 秒")

audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
print(f"  numpy 数组: shape={audio.shape} dtype={audio.dtype}")

# ---- STEP 3: 加载模型 ----
print()
print("-" * 72)
print("STEP 3: 加载模型（已缓存，应很快）")
print("-" * 72)
from faster_whisper import WhisperModel

t0 = time.time()
model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=8)
t_load = time.time() - t0
print(f"  OK，加载耗时 {t_load:.2f} 秒")

# ---- STEP 4: 转写 ----
print()
print("-" * 72)
print("STEP 4: 转写")
print("-" * 72)
t0 = time.time()
segments, info = model.transcribe(
    audio,                      # ← 传 numpy 数组，跳过 decode_audio
    language="zh",
    beam_size=5,
    vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=500),
)
print(f"  语言         : {info.language} (置信度 {info.language_probability:.2f})")
print(f"  音频时长     : {info.duration:.1f} 秒")
tr_segs = list(segments)
t_tr = time.time() - t0

print()
print("  --- 转写结果 ---")
for s in tr_segs:
    print(f"  [{s.start:6.2f} -> {s.end:6.2f}]  {s.text.strip()}")

full = "".join(s.text.strip() for s in tr_segs)

# ---- STEP 5: 统计 ----
print()
print("=" * 72)
print("结果")
print("=" * 72)
print(f"  模型        : {MODEL} (int8, CPU, 8 线程)")
print(f"  音频时长    : {info.duration:.1f} 秒")
print(f"  转写耗时    : {t_tr:.1f} 秒")
if t_tr > 0:
    ratio = info.duration / t_tr
    print(f"  ** 实时倍率 : {ratio:.2f}x  (>1 = 比实时快)")
    print(f"  ** 1 小时音频预计: {3600 / ratio / 60:.1f} 分钟")
print(f"  片段数      : {len(tr_segs)}")
print(f"  字符数      : {len(full)}")
print()
print("  --- 全文 ---")
print(full)

# ---- STEP 6: opencc 繁转简 ----
try:
    import opencc
    cc = opencc.OpenCC("t2s")
    simp = cc.convert(full)
    print()
    if simp != full:
        print("  --- 简体化后（有变化）---")
        print(simp)
    else:
        print("  opencc: 本来就是简体")
except Exception as e:
    print(f"  opencc 失败: {e}")

# 清理
try:
    os.remove(WAV)
except Exception:
    pass
