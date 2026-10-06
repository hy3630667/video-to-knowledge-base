# -*- coding: utf-8 -*-
"""
配置模板 —— 复制为 config.py 后按需修改。

字段留空或删掉 = 走环境变量或自动探测。

  复制：  cp config.example.py config.py
  Windows：copy config.example.py config.py

⭐ config.py 已在 .gitignore 里，不会被提交。
"""

# ---- 外部命令 ----
# 留空则用 PATH 上的同名命令（推荐：把 ffmpeg / node 加到 PATH）
# 如果不在 PATH 上，填绝对路径，例如：
#   Windows:  NODE = r"C:\Program Files\nodejs\node.exe"
#   macOS:    NODE = "/usr/local/bin/node"
NODE = ""
FFMPEG = ""
FFPROBE = ""

# ---- dify-mcp 目录 ----
# 本仓库不含 dify-mcp 源码，需要单独获取：
#   https://github.com/<作者>/dify-mcp   （或用你自己的 Dify API 调用方式）
# 指向含 bin/difywf.js 的目录
DIFY_MCP_DIR = ""

# ---- 服务地址 ----
# 抖音/B站抓取服务（Douyin_TikTok_Download_API）
DOUYIN_API = "http://localhost:8080"
# 小红书抓取服务（XHS-Downloader，python main.py API）
XHS_API = "http://127.0.0.1:5556/xhs/detail"
# Dify 地址
DIFY_BASE = "http://localhost"

# ---- 小红书 Cookie 文件 ----
# ⚠️ 含登录凭据，不要提交到 Git
# 格式：一行 Cookie 头，形如 a1=xxx; web_session=yyy; ...
XHS_COOKIE_FILE = ""

# ---- 工作目录 ----
# 下载的视频、临时音频都放这里
WORK_DIR = ""

# ---- Dify 目标知识库 ID ----
# 在 Dify 知识库页面 URL 里：/datasets/<这个 ID>/documents
DEFAULT_DATASET = ""

# ---- Dify 纠错工作流 app id ----
# 从 workflows/01-transcript-correction.yml 导入后，
# 在应用 URL 里：/app/<这个 ID>/workflow
CORRECTION_APP_ID = ""
