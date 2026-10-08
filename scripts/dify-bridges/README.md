# dify-bridges

这一组是**桥接脚本**：主脚本 `one-click-pipeline.py` 通过它们操作 Dify。
它们建立在 **dify-mcp**（Dify 的 MCP / CLI 工具，需自行获取）之上，
复用了它的登录态与 HTTP 封装（`src/core/*`）。

---

## ⚠️ 放哪里

`one-click-pipeline.py` 是按 **`DIFY_MCP_DIR`** 这个配置项去找它们的：

```python
# scripts/config.py
DIFY_MCP_DIR = r"<你的 dify-mcp 目录>"
```

所以有两条路：

| 方式 | 做法 |
|---|---|
| **A（推荐）** | 把这些 `.mjs` 复制进你的 `dify-mcp` 目录，再把 `DIFY_MCP_DIR` 指向那里 |
| **B** | 把 `DIFY_MCP_DIR` 直接指向本目录，但这样脚本就 `import` 不到 dify-mcp 的 `src/core/*`，**不可用** |

**→ 用方式 A。** 这些脚本 `import` 了 `./src/core/http.ts`、`./src/core/auth.ts`，
必须和 dify-mcp 的源码放在同一个目录树里。

---

## 每个脚本做什么

| 脚本 | 谁调它 | 作用 |
|---|---|---|
| `_run-correction.mjs` | `correct_transcript()` | 调**纠错工作流**（`workflows/01-*.yml`），返回校对稿/知识点/问答对 |
| `_add-document.mjs` | `write_to_dify()` | 用一段文本**创建知识库文档**（自定义分段）|
| `_upload-file.mjs` | `write_to_dify()` | **上传文件**拿 file id |
| `_bind-topics.mjs` | 手工 / 入库后 | 按文档名规则给文档**绑「主题」元数据** |
| `_fix-topic-prompt.mjs` | 手工 / 新增视频后 | 按知识库**实际内容**重建主题判断提示词 |
| `_probe-hit.mjs` | `evaluation/eval.py` | **检索探针**：看某个问题实际命中哪些片段、排第几 |
| `_export-docs.mjs` | 手工 | **导出知识库**（文档 + 全部分段），兼当备份 |

---

## ⭐ `_upload-file.mjs` 为什么必须存在

原来上传是把 payload（含 base64 内容）当作**命令行参数**传：

```python
dify_cli(["file", "upload", "--file", file_meta, ...])   # ← base64 走命令行
```

内容一大，base64 就爆 **Windows 的 32,767 字符命令行上限**：

| 内容 | base64 | 结果 |
|---|---|---|
| 3091 字 | ≈12K | ✅ 通过 |
| 9178 字 | **≈36.7K** | ❌ `FileNotFoundError: [WinError 206] 文件名或扩展名太长` |

**实测 `--file` 不支持 `@file`，也不支持 `-`（stdin），只认内联 JSON** —— 两条常规绕法都走不通。

**→ 这个桥接改成：payload 写文件 → 从文件读 → 直接调 `POST /console/api/files/upload`（multipart）。**
实测 33,140 字符的 base64 上传成功。

---

## 配置

这些脚本里的知识库 ID / 应用 ID **全部走环境变量**，没有硬编码：

| 环境变量 | 用在哪 |
|---|---|
| `DIFY_DATASET_ID` | `_bind-topics.mjs` / `_probe-hit.mjs` / `_export-docs.mjs` |
| `DIFY_TOPIC_METADATA_ID` | `_bind-topics.mjs`（「主题」元数据字段 id）|
| `DIFY_QA_APP_ID` | `_fix-topic-prompt.mjs`（问答 chatflow 的 app id）|
| `DIFY_BASE_URL` | 全部（默认 `http://localhost`）|

登录态复用 dify-mcp 的 `~/.difywf/hosts.json`，**本项目不另存任何凭据**。

---

## 用之前先确认

```powershell
# 1. dify-mcp 已登录
node <dify-mcp>/bin/difywf.js knowledge list-documents --dataset-id <你的知识库ID>

# 2. 导出能跑（最安全的连通性自检，只读）
node _export-docs.mjs
```
