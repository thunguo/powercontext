# PowerContext 问题审计与可贡献点

本文记录一次针对当前 `master`（`9750e87`）的代码阅读、单测、本地 OceanBase CE 以及 DeepSeek V4 Flash 0731 实测。只收录**已经用复现代码跑通**的问题；未复现或被证伪的猜想单独列出，避免把设计选择误写成缺陷。

复现入口：`scripts/issue_audit/repro.py`。原始结果见 `scripts/issue_audit/results.json`。不要把 API Key 写进仓库或文档。

## 测试环境

| 项 | 值 |
| --- | --- |
| 代码 | `9750e87 feat(tracing): add memory read-path spans` |
| Python | 3.12.3 + 仓库 `.venv`（`uv sync`） |
| OceanBase | Docker Compose 临时实例 `ghcr.io/oceanbase/oceanbase-ce:4.3.5.6-106000012026040916`，`MODE=slim`，租户 `root@test`，库 `powercontext`，端口 `2881` |
| 模型 | `deepseek:deepseek-v4-flash`（服务端对应 DeepSeek-V4-Flash-0731），`DEEPSEEK_API_KEY` 仅注入进程环境 |
| 未跑 | Codex / Bub / Harbor 长程 workload、真实 Embedding 供应商、Vec1 原生扩展 |

OceanBase 连接串（密码仅用于本地临时容器，测试后销毁）：

```text
mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4
```

官方 OceanBase 门禁测试在该实例上结果为：**31 passed, 3 skipped**（跳过项是缺少 SQLite Vec1 扩展的 SQLite vector/hybrid 用例）。OceanBase FTS / vector / hybrid 并发检索均通过。

DeepSeek 直连 Chat Completions 返回 `model=deepseek-v4-flash`，HTTP 200。PowerContext `PromptedOutput` 结构化 Memory 抽取成功；`POST /v1/sources/content` → `POST /v1/memory/flush` → `POST /v1/memory/search` → `POST /v1/context/prepare` 全链路约 4.4s，抽取出含 OceanBase / HTAP 的 Memory，并能编成 PreparedContext。

---

## 已确认缺陷

### 1. OceanBase FTS 丢弃短 token，与 SQLite 召回不一致

**严重度：高。** Analyzer v1 会把 `uv` 保留为独立词。SQLite FTS5 能用查询 `uv` 命中 `"Use uv for dependency management."`；同一条 Memory 在 OceanBase `WITH PARSER SPACE` 的 FULLTEXT 上，`uv` 召回为空，但更长的 `dependency management` 能命中。CJK 查询 `OceanBase HTAP` 两边都能命中，因为 Analyzer 已把汉字展开为 `u_*` / `b_*` 长 token。

用户若把项目偏好写成 “use uv / go / js”，在 OceanBase 部署上会静默检索失败。

**复现：**

```python
import asyncio
from pydantic import SecretStr
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig, RememberMemoryRequest, SearchMemoryRequest, open_builtin_runtime,
)

ENTRIES = (
    MemoryEntryInput(kind="decision", text="项目选择 OceanBase，因为需要 HTAP。"),
    MemoryEntryInput(kind="preference", text="Use uv for dependency management."),
)

async def recall(database):
    async with open_builtin_runtime(BuiltinConfig(database=database)) as runtime:
        memory = runtime.memory.for_scope("fts-parity")
        await memory.remember(RememberMemoryRequest(entries=ENTRIES))
        cjk = await memory.search(SearchMemoryRequest(query="OceanBase HTAP", mode="fts"))
        short = await memory.search(SearchMemoryRequest(query="uv", mode="fts"))
        return [hit.text for hit in cjk.hits], [hit.text for hit in short.hits]

sqlite_cjk, sqlite_uv = asyncio.run(recall(SQLiteConfig(url="sqlite+aiosqlite:////tmp/fts-parity.db")))
ob_cjk, ob_uv = asyncio.run(recall(OceanBaseConfig(url=SecretStr(
    "mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4"
))))
print({"sqlite_cjk": sqlite_cjk, "sqlite_uv": sqlite_uv, "ob_cjk": ob_cjk, "ob_uv": ob_uv})
```

**实测：**

```text
sqlite_cjk: ["项目选择 OceanBase，因为需要 HTAP。"]
sqlite_uv:  ["Use uv for dependency management."]
ob_cjk:     ["项目选择 OceanBase，因为需要 HTAP。"]
ob_uv:      []
```

**可贡献修复：** OceanBase 检索应复用 Analyzer token，而不是把 `analyzed_query` 直接丢给 NATURAL LANGUAGE `MATCH ... AGAINST`。可选路径：BOOLEAN MODE 加引号 token（与 SQLite `fts_match_query` 对齐）、或对短 token 走精确过滤。需要回归：`uv`、`go`、单汉字（已展开）、以及现有英文长词。

---

### 2. 推理就绪探针硬编码 2 秒，真实 DeepSeek 调用会抖动成 `degraded`

**严重度：高。** `READINESS_PROBE_TIMEOUT_SECONDS = 2.0`，与 `POWERCONTEXT_SERVER_INFERENCE_GENERATION_TIMEOUT_SECONDS`（默认 30）无关。探针会发一次真实 `model.request(...)`。

同一次环境、同一个 `deepseek-v4-flash` Key：

| 运行 | `/health/ready` | `inference.generation` | 耗时 |
| --- | --- | --- | --- |
| 1 | `ready` | `ready` | 1007 ms |
| 2 | `degraded`（HTTP 仍 200） | `timeout` | 2037 ms |
| 3（flush E2E） | `ready` | `ready` | 随后抽取 4.4s 成功 |

模型并没有坏，只是偶发超过 2s。`degraded` 会被缓存 30s，Dashboard / `doctor` / 编排系统会把一次慢探针当成推理不可用。

**复现：**

```python
from fastapi.testclient import TestClient
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime.config import InferenceConfig
from powercontext.server.factory import create_server_app
from powercontext.server.settings import ServerSettings

settings = ServerSettings(
    database=SQLiteConfig(url="sqlite+aiosqlite:////tmp/ready.db"),
    inference=InferenceConfig(
        generation_model="deepseek:deepseek-v4-flash",
        generation_timeout_seconds=90,
    ),
)
with TestClient(create_server_app(settings=settings)) as client:
    print(client.get("/health/ready").json())
```

**实测（第二次）：**

```json
{
  "status": "degraded",
  "checks": {
    "runtime": "ready",
    "database": "ready",
    "inference.generation": "timeout"
  }
}
```

**可贡献修复：** 探针超时跟 `generation_timeout_seconds` 走，或单独配置并给一个更合理的下限（例如 10–30s）。超时不应把“慢”和“不可用”混成同一档。

---

### 3. MCP 对 Agent 隐藏 flush / prepare / generate，缺参时泄漏 HTTP 422

**严重度：高。** MCP 只投影 OpenAPI 里的一小段 operation。实测 tool 列表没有 `flush_memory`、`prepare_context`、`generate_experience`、`generate_skill`、`get_capabilities`。走 MCP 的 Agent 可以 `remember_memory` / `search_memory`，但不能触发 Source 窗口抽取，也不能 `prepare_context`。

另外：`initialize` 缺少 `protocolVersion` 时返回 JSON-RPC `-32602`，同时把 28 条 Pydantic 校验错误打到日志；`tools/call search_memory` 不带 body 时，FastMCP 把 FastAPI 422 包装成 `isError` 文本，而不是 MCP 的 invalid-params。

**复现：**

```python
from fastapi.testclient import TestClient
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.server.factory import create_server_app
from powercontext.server.settings import ServerSettings

app = create_server_app(settings=ServerSettings(
    database=SQLiteConfig(url="sqlite+aiosqlite:////tmp/mcp.db"),
))
headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
with TestClient(app) as client:
    missing = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"capabilities": {}, "clientInfo": {"name": "repro", "version": "0"}},
    })
    ok = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "repro", "version": "0"},
        },
    })
    session = ok.headers["mcp-session-id"]
    client.post("/mcp", headers={**headers, "mcp-session-id": session},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = client.post("/mcp", headers={**headers, "mcp-session-id": session},
                        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    bad = client.post("/mcp", headers={**headers, "mcp-session-id": session}, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "search_memory", "arguments": {}},
    })
    print(missing.json()["error"], [t["name"] for t in tools.json()["result"]["tools"]], bad.json())
```

**实测：**

- 缺 `protocolVersion`：`{"code": -32602, "message": "Invalid request parameters"}`，进程日志刷 28 条 `Failed to validate request`。
- 有效 initialize 的 `protocolVersion` 回显为 `2025-03-26`。
- `notifications/initialized` HTTP 202。
- Tool 集合含 handoff / memory CRUD / candidate review，**不含** `flush_memory` 与 `prepare_context`。
- 空参数 `search_memory`：`isError: true`，文本为 `HTTP error 422: Unprocessable Entity - {'error': {'code': 'invalid_request', ... 'loc': ['body']}}`。

**可贡献修复：** 把 Source 抽取和 context prepare 纳入 MCP 白名单；在 OpenAPI→MCP 层把校验失败映射为 JSON-RPC 参数错误；降低 initialize 校验的日志噪音。

---

### 4. 根级 `--json` 对 `doctor` 无效

**严重度：中。** `powercontext --json` 只写入 Client 内容命令的 overrides。`doctor` 是独立 Typer 子应用，必须写 `powercontext doctor --json`。`powercontext --json doctor` 仍输出人类可读文本。

**复现：**

```python
from typer.testing import CliRunner
from powercontext.cli.app import create_cli
from powercontext.cli.system import doctor_app

cli = create_cli([doctor_app])
root = CliRunner().invoke(cli, ["--json", "doctor"])
nested = CliRunner().invoke(cli, ["doctor", "--json"])
print(root.output.splitlines()[0])
print(nested.output.splitlines()[0])
```

**实测：**

```text
# powercontext --json doctor
package: ok - powercontext 0.0.1

# powercontext doctor --json
{
  "ok": false,
  "status": "failed",
  ...
}
```

**可贡献修复：** 让根 callback 的 `--json` 向下传递，或在 `--help` 里写明它只作用于内容命令。

---

### 5. CLI 文档链接指向不存在的 `main` 分支

**严重度：低。** `src/powercontext/cli/app.py` 的 `DOCUMENTATION_URL` 使用 `/tree/main/docs/en/docs`。仓库默认分支是 `master`（`pyproject.toml` 与 Codex plugin 已用 master）。

**复现：**

```python
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from powercontext.cli.app import DOCUMENTATION_URL

def status(url: str) -> int:
    try:
        with urlopen(Request(url, headers={"User-Agent": "audit"}), timeout=20) as response:
            return response.status
    except HTTPError as error:
        return error.code

print(DOCUMENTATION_URL, status(DOCUMENTATION_URL))
print(status(DOCUMENTATION_URL.replace("/tree/main/", "/tree/master/")))
```

**实测：** `.../tree/main/docs/en/docs` → **404**；`.../tree/master/docs/en/docs` → **200**。`ISSUES_URL` 为 200。

---

### 6. 未匹配路由使用 FastAPI `{"detail": "Not Found"}`，而不是契约错误信封

**严重度：中。** 公开契约错误形如 `{"error": {"code", "message", "details"}}`。`POST /v1/memory/{id}/extract` 和 `GET /v1/does-not-exist` 都返回 FastAPI 默认 404。`/docs`、`/redoc`、`/openapi.json` 可访问但不在 canonical OpenAPI `paths` 里；`/v1/openapi.json` 为 404。

抽取的正式入口是 `POST /v1/memory/flush`（实测 200），并不是缺失的 `{memory_id}/extract`。真正的缺陷是 **404 信封与契约不一致**，以及文档/SDK 生成器若去猜 REST 风格 extract 路径会踩空。

**复现：**

```python
from fastapi.testclient import TestClient
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.server.factory import create_server_app
from powercontext.server.settings import ServerSettings

with TestClient(create_server_app(settings=ServerSettings(
    database=SQLiteConfig(url="sqlite+aiosqlite:////tmp/http.db"),
))) as client:
    print(client.post("/v1/memory/mem-1/extract", json={"scope_id": "demo"}).json())
    print(client.get("/v1/does-not-exist").json())
    print(client.post("/v1/memory/flush", json={"scope_id": "demo"}).status_code)
    print(client.get("/docs").status_code, "/docs" in client.get("/openapi.json").json()["paths"])
```

**实测：**

```text
{"detail": "Not Found"}     # extract
{"detail": "Not Found"}     # unknown
200                         # flush
200 False                   # /docs 存在但不在契约 paths 中
```

---

### 7. `httpx.TimeoutException` 未映射为 `InferenceTimeoutError`

**严重度：中。** `_map_error` 只把内建 `TimeoutError` 和部分 `ModelHTTPError` 收成 timeout。底层 httpx 超时会原样抛出，HTTP 层变成 `internal_error` 500，而不是可重试的推理超时。

**复现：**

```python
import httpx
from powercontext.builtin.inference.pydantic_ai import _map_error

assert _map_error(
    httpx.TimeoutException("connect timed out"),
    operation="generate",
    timeout_seconds=30.0,
) is None
```

**实测：** `mapped is None`。

---

### 8. 默认 Memory evidence projector 不包含 `ContentSource.content`

**严重度：中（自定义 pipeline 陷阱）。** Runtime 组装时注入了 `_ContentEvidenceProjector`，所以 Server 路径会把正文送给模型（DeepSeek flush E2E 已证明）。但 `LLMMemoryCandidatePipeline(...)` 默认使用 `DefaultMemoryEvidenceProjector`，只投影 `name` / `materialization` / `description`。测试和外部集成若漏传 projector，模型看不到证据正文，抽取会空转。

**复现：**

```python
from powercontext import SourceMaterialization
from powercontext.builtin.artifacts.memory.extraction import DefaultMemoryEvidenceProjector
from powercontext.builtin.runtime.composition import _ContentEvidenceProjector
from powercontext.builtin.sources import ContentSource

source = ContentSource(
    name="note-1",
    materialization=SourceMaterialization.CAPTURED,
    content="The project selected OceanBase because of HTAP.",
)
print(DefaultMemoryEvidenceProjector().project_source(source))
print(_ContentEvidenceProjector().project_source(source))
```

**实测：**

```text
{'name': 'note-1', 'materialization': 'captured', 'description': None}
{'source_type': 'content', 'source_id': 'note-1', 'content': 'The project selected OceanBase because of HTAP.', 'metadata': {}}
```

**可贡献修复：** 把 Content 正文投影做成默认行为，或在未注入 projector 时拒绝 ContentSource。

---

## 已用 DeepSeek V4 Flash 0731 验证、不是缺陷的路径

这些路径在本环境是通的，不要当成 bug 修。

1. **结构化 Memory 抽取。** `deepseek:deepseek-v4-flash` + `PromptedOutput(MemoryExtractionOutput)` 一次成功，时延 3086 ms，产出 1 条含 OceanBase/HTAP 的 candidate。
2. **HTTP 抽取闭环。** capture 202 → flush `processed` → search 命中抽取文本 → prepare 返回 `powercontext.prepared-context.v1`。
3. **最小探针。** 单独 `probe_pydantic_ai_model(..., timeout_seconds=2)` 有时 965 ms 成功；与第 2 条合看，说明 2s 预算贴着真实延迟，而不是模型不可用。
4. **OceanBase 基础能力。** MySQL 兼容租户、`utf8mb4`、建表、remember、CJK FTS、vector/hybrid（合成 3 维 embedding）以及官方并发检索测试均通过。

DeepSeek 当前没有在 PowerContext 里配置 embedding。`/v1/capabilities.search_modes` 在只配 generation 时是 `["auto", "fts"]`。这是预期，不是回归。

---

## 被证伪或不应当缺陷报的点

| 猜想 | 结论 |
| --- | --- |
| Bearer 比较存在时序攻击 | `StaticBearerMiddleware` 已用 `secrets.compare_digest` |
| Dashboard `display_name` XSS | `dashboard.js` 使用 `textContent` / `setText`，没有 `innerHTML` |
| 全局 CORS `*` | 代码中没有 `CORSMiddleware` |
| 认证 401 缺少 `WWW-Authenticate` | 中间件已设置 `WWW-Authenticate: Bearer` |
| `POST /v1/memory/{id}/extract` 是遗漏的产品 API | 契约入口是 `POST /v1/memory/flush`；问题是 404 信封和 MCP 没暴露 flush |
| DeepSeek V4 无法做结构化输出 | 实测可以 |
| OceanBase CE slim 不能建 vector index | 合成 embedding 下 vector/hybrid 搜索成功 |

Dashboard 把 token 放进 `sessionStorage`、CSP 带 `script-src 'unsafe-inline'`，属于明确的产品取舍，可改进但不是静默功能错误。

---

## 可贡献点（按杠杆排序）

1. **OceanBase FTS 与 Analyzer 对齐**（问题 1）。这是 SQLite / OceanBase 行为分叉，直接影响 OceanBase 作为默认生产库的可信度。
2. **MCP 补齐 flush + prepare**（问题 3）。否则 MCP 宿主（Claude Code / 其它 Agent）只能手写 Memory，跑不通 “capture → extract → recall”。
3. **就绪探针超时可配置**（问题 2）。云模型延迟抖动会让健康检查撒谎。
4. **统一错误信封**（问题 6）和 **MCP 校验错误映射**（问题 3）。客户端现在要同时解析 `error` 与 `detail`。
5. **CLI `--json` 传递与文档 URL**（问题 4、5）。改动面小，适合第一份贡献。
6. **推理错误映射补 `httpx.TimeoutException` / OpenAI `APITimeoutError`**（问题 7）。
7. **文档：DeepSeek 只覆盖 generation。** Embedding / hybrid 需要第二个 provider；`deepseek:deepseek-v4-flash` 已在 pydantic-ai `KnownModelName` 中，官方 extra 用 `openai` 即可。
8. **默认 evidence projector 包含 Content 正文**（问题 8）。
9. **产品规划中已点名、代码尚未落地的方向**（2026-08-16 周会）：Go / TypeScript 实现、更多 Agent 宿主（Hermes、pi、Dify）、Langfuse 一类观测后端、评测指标补充。这些不是回归，但是高价值贡献面。
10. **SQLite Vec1 未随包分发。** 没有 `POWERCONTEXT_VEC1_EXTENSION` 时 SQLite 不能做 vector/hybrid（本环境官方测试因此 skip）。可贡献：检测失败时的诊断、文档安装矩阵、或可选 wheel。

---

## 如何重跑

```bash
# 1. 临时 OceanBase（测试结束后 down -v）
docker compose -f /tmp/oceanbase-compose.yaml up -d   # 或仓库 e2e/bub/compose.oceanbase.yaml 并发布 2881
export POWERCONTEXT_TEST_OCEANBASE_URL='mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4'

# 2. DeepSeek
export DEEPSEEK_API_KEY=...   # 不要提交

# 3. 审计复现
uv run python scripts/issue_audit/repro.py

# 4. 官方 OceanBase 门禁
uv run python -m pytest tests/builtin/persistence/test_oceanbase_profile.py \
    tests/e2e/test_runtime_server.py tests/e2e/test_memory_search_concurrency.py \
    tests/e2e/test_statistics_flow.py tests/e2e/test_candidate_review.py

# 5. 销毁
docker compose -f /tmp/oceanbase-compose.yaml down -v
```

`scripts/issue_audit/repro.py` 不会打印密钥。`confirmed: false` 在 DeepSeek 抽取那一项表示**抽取成功、不是缺陷**；就绪探针和 OceanBase 短 token 两项为 `confirmed: true`。
