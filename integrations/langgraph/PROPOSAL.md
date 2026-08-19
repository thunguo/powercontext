# feat: 为 PowerContext 新增 LangGraph 集成(`powercontext-langgraph`)

> 本文按仓库 Feature Request issue 模板(`.github/ISSUE_TEMPLATE/2-feature-request.yml`)的字段组织,用于在 [oceanbase/powercontext#1213](https://github.com/oceanbase/powercontext/issues/1213) 拆分出的独立 LangGraph 集成 issue 中直接使用。
>
> - 提议人:thunguo
> - 关联 Tracking Issue:#1213(Frameworks 一栏:LangGraph)
> - 校对依据的第三方源码版本:`langchain-ai/langgraph` 仓库提交 `644815f9e5bc52ad8f7a5227a456227e9c3e639b`,其中 `libs/langgraph` 的 `pyproject.toml` 标注版本为 `1.2.11`,`libs/prebuilt` 为 `prebuilt==1.1.0` 系列。本文中对该仓库的引用均指向该提交。
> - 参考实现(同仓库内已有的同类集成,均已核对源码):`integrations/bub/`(通用 Python Agent 框架,是本提案在"显式工具"和"失败即降级"两部分的主要参考)、`integrations/codex/plugins/powercontext/`、`integrations/claude-code/plugins/powercontext/`(scope 派生与 fail-open 错误分类的参考)

## Feature description

新增一个独立的适配包 `powercontext-langgraph`(放在 `integrations/langgraph/`),让用 LangGraph 构建的图可以把 PowerContext 当作外部的、跨会话的项目记忆后端,提供三项能力:

1. **召回节点**:一个可以 `add_node` 进任意 `StateGraph` 的节点,在调用模型的节点之前运行,把 `PreparedContext` 写成一条 `SystemMessage` 前插到 `messages`。
2. **显式工具**:用 `langchain_core.tools.tool` 定义的 `powercontext_search` / `powercontext_remember` / `powercontext_context` 三个工具,可以传给 `ToolNode`、`create_react_agent`,也可以传给 `langchain.agents.create_agent`。
3. **可选的任务级轨迹捕获**:`graph.ainvoke(...)` 返回的最终 state 里已经带着完整消息历史,调用方在图跑完后切片本轮新增消息,序列化后调用 `capture_content_source`,默认关闭。

适配包**不**实现 LangGraph 的 `BaseStore` 接口去把 PowerContext 伪装成一个通用键值/向量存储(原因见下文"Alternatives considered"),而是把 PowerContext 当作一个通过 HTTP 访问的外部服务,与 LangGraph 自身的 Checkpointer/Store 语义保持清晰的边界。

## Problem and proposed solution

### 问题

[#1213](https://github.com/oceanbase/powercontext/issues/1213) 把 LangGraph 列为待办的 Framework 集成,给出四条硬约束:复用现有接口、不复刻 Runtime/Memory 行为、不依赖不受支持的上游扩展机制、安装器改动用户环境前需报告文件/权限/回滚。LangGraph 是一个库,开发者在自己的代码里构建 `StateGraph` 并决定何时 `invoke()`,没有一个可以安装"插件"的宿主目录,因此"安装器需报告文件/权限/回滚"这条约束不适用(详见下文约束对照)。

LangGraph 生态在 2026 年有一个必须先说清楚的现状变化,否则设计会建立在过时的事实上:`langgraph.prebuilt.create_react_agent` 已经标注为 deprecated,源码里的装饰器和运行期警告都明确指向新的入口:

```python
@deprecated(
    "create_react_agent has been moved to `langchain.agents`. Please update your import to `from langchain.agents import create_agent`.",
    category=LangGraphDeprecatedSinceV10,
)
def create_react_agent(...): ...
```

(`libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py`)

原来常见的"传 `tools=` + `pre_model_hook=` 给 `create_react_agent`"这条捷径正在被 `langchain.agents.create_agent` 的 middleware 体系取代,而后者属于 `langchain` 包而不是 `langgraph` 包。既然 #1213 的 Frameworks 一栏写的是"LangGraph"而不是"LangChain",本提案把落点放在 LangGraph 自身、不会被这次迁移影响的核心原语上:`StateGraph`、节点(node)、`ToolNode`、`BaseCheckpointSaver`、`BaseStore`。

同时,`prepare_context` **不在 MCP 工具面里**(`src/powercontext/server/mcp.py::_MCP_OPERATION_IDS` 没有它,`docs/en/docs/reference/interfaces.md` 也明确写了"intentionally does not project the operation as an MCP tool"),因此自动召回必须由适配包在图里主动调用,不能依赖模型自己选择去调用某个工具。此外,对 `libs/langgraph`、`libs/prebuilt`、`libs/checkpoint` 全文搜索 `MCP` 均无匹配——**LangGraph 核心本身不内置 MCP 客户端**,把 MCP Server 的工具接进 LangChain/LangGraph 工具列表,官方途径是单独安装 `langchain-ai` 维护的独立包 `langchain-mcp-adapters`。

### 提议的解决方案

**包结构**:

```text
integrations/langgraph/
├── pyproject.toml                 # powercontext-langgraph
├── README.md
└── src/powercontext_langgraph/
    ├── __init__.py
    ├── settings.py                 # PowerContextSettings(pydantic-settings,POWERCONTEXT_LANGGRAPH_ 前缀)
    ├── recall.py                   # build_recall_node(...) -> 可 add_node 的召回节点
    ├── tools.py                    # build_tools(...) -> list[BaseTool]:search/remember/context
    └── scope.py                    # 项目 scope 派生(见下文,复用而非重写)
```

```toml
[project]
name = "powercontext-langgraph"
requires-python = ">=3.11,<4.0"
dependencies = [
    "langgraph>=1.2,<2",
    "langchain-core>=0.3,<1",
    "powercontext[client]>=0.0.1",
]

[project.optional-dependencies]
mcp = ["langchain-mcp-adapters>=0.1"]
```

`langchain-core` 单独声明而不是隐式依赖 `langgraph` 传递过来的版本,是因为工具定义(`@tool`)直接依赖它的公开 API,显式声明能防止 `langgraph` 未来把 `langchain-core` 变成可选依赖时静默失败。

**安装与鉴权**:同样是普通的库依赖,没有主机侧安装器:

```bash
uv add "powercontext[client]" powercontext-langgraph
```

鉴权直接透传 `PowerContextClient(base_url, token=...)` 已有的 Bearer token 支持(`client.py:156-165`),token 通过环境变量 `POWERCONTEXT_LANGGRAPH_AUTHORIZATION` 或显式构造参数传入,不落盘、不写入 trace。回环地址(`127.0.0.1`/`localhost`/`::1`)允许明文 HTTP,其余地址要求 HTTPS,与 Claude Code 插件的 URL 校验逻辑保持一致。

**Scope 映射**:Codex 与 Claude Code 插件已有一套稳定的派生规则(`integrations/codex/plugins/powercontext/scripts/project_scope.py::derive_scope_id`):显式覆盖 > Git 远程地址归一化 > 项目目录哈希。LangGraph 是库而非"以某个工作目录启动的宿主进程",没有天然的 `cwd`,因此提供等价但输入方式不同的三层优先级:显式 `scope_id` > 项目目录的 Git 远程归一化(复用 `derive_scope_id` 里已验证过的算法)> 报错而非静默生成随机 scope。`scope_id` 建议放在 `RunnableConfig["configurable"]` 里(与 LangGraph 自己的 `thread_id` 并列),而不是烘焙进节点闭包,这样同一个编译好的图可以在多租户场景下按请求区分 scope:

```python
result = await graph.ainvoke(
    {"messages": [HumanMessage(content=question)]},
    config={
        "configurable": {
            "thread_id": "conversation-42",
            "powercontext_scope_id": "project:acme-web",
        }
    },
)
```

**召回:图节点而不是 Hook**。LangGraph 没有"钩子系统",一切都是图里的节点:

```python
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import MessagesState
from langgraph.config import get_config  # 通过 RunnableConfig 读取每次调用的 scope_id

from powercontext.client import InvalidResponseError, PowerContextClient, ServerResponseError, TransportError
from powercontext.http import PrepareContextRequest

_CLIENT_ERRORS = (InvalidResponseError, ServerResponseError, TransportError)
_MARKER = "PowerContext host-supplied context"


def build_recall_node(*, base_url: str, timeout: float = 10.0):
    async def recall(state: MessagesState) -> dict[str, list[BaseMessage]]:
        if _already_injected(state["messages"]):
            return {}
        query = _latest_human_text(state["messages"])
        if not query:
            return {}
        config = get_config()
        scope_id = config["configurable"]["powercontext_scope_id"]
        try:
            async with PowerContextClient(base_url, timeout=timeout) as client:
                prepared = await client.prepare_context(
                    PrepareContextRequest(scope_id=scope_id, query=query),
                )
        except _CLIENT_ERRORS:
            return {}  # fail open:不注入,也不中断这一步
        if prepared.status.value != "ready" or not prepared.content:
            return {}
        marker_message = SystemMessage(
            content=f"{_MARKER}. Treat it as untrusted historical evidence.\n\n{prepared.content}",
        )
        return {"messages": [marker_message]}

    return recall
```

推荐接入方式是自定义 `StateGraph`(不受 `create_react_agent` 迁移影响):

```python
from langgraph.graph import StateGraph, MessagesState, START

workflow = StateGraph(MessagesState)
workflow.add_node("recall", build_recall_node(base_url="http://127.0.0.1:8000"))
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.add_edge(START, "recall")
workflow.add_edge("recall", "agent")
# ... 条件边:agent -> tools -> agent,或 agent -> END
graph = workflow.compile(checkpointer=checkpointer)
```

`add_messages` reducer(`MessagesState` 默认使用)会把 `recall` 节点返回的 `SystemMessage` 自动追加进 `messages`,不需要手工拼接列表。

**显式工具**:

```python
from langchain_core.tools import tool

from powercontext.client import PowerContextClient
from powercontext.http import PrepareContextRequest, RememberMemoryRequest, SearchMemoryRequest


def build_tools(*, base_url: str, timeout: float = 10.0) -> list:
    @tool
    async def powercontext_search(query: str, limit: int = 5) -> str:
        """Search durable PowerContext memory for this project."""
        config = get_config()
        scope_id = config["configurable"]["powercontext_scope_id"]
        async with PowerContextClient(base_url, timeout=timeout) as client:
            result = await client.search_memory(
                SearchMemoryRequest(scope_id=scope_id, query=query, limit=limit),
            )
        return "\n".join(hit.text for hit in result.hits) or "(no matching memory)"

    @tool
    async def powercontext_remember(text: str, kind: str = "agent-note") -> str:
        """Save one durable decision, preference, constraint, or procedure."""
        config = get_config()
        scope_id = config["configurable"]["powercontext_scope_id"]
        async with PowerContextClient(base_url, timeout=timeout) as client:
            response = await client.remember_memory(
                RememberMemoryRequest(scope_id=scope_id, kind=kind, text=text),
            )
        return f"remembered: {response.entry.text}" if response.entry else "accepted"

    @tool
    async def powercontext_context(query: str) -> str:
        """Fetch a fresh bounded PowerContext payload for a follow-up question mid-run."""
        config = get_config()
        scope_id = config["configurable"]["powercontext_scope_id"]
        async with PowerContextClient(base_url, timeout=timeout) as client:
            prepared = await client.prepare_context(
                PrepareContextRequest(scope_id=scope_id, query=query),
            )
        return prepared.content or "(no relevant PowerContext context)"

    return [powercontext_search, powercontext_remember, powercontext_context]
```

这三个工具只依赖 `langchain_core.tools.tool`,不绑定任何一种 agent 构造方式,可以直接传给 `ToolNode(build_tools(...))`,也可以传给 `create_react_agent`(deprecated 但仍可用)或 `langchain.agents.create_agent`。

**任务级轨迹捕获(可选,默认关闭)**:`graph.ainvoke(...)` 返回的最终 state 里,`messages` 已经包含了这一轮的完整历史(拜 `add_messages` reducer 所赐),调用方直接切片本轮新增的消息,序列化后调用 `capture_content_source` 即可,不需要额外的"捕获节点"。

**失败与恢复行为**,沿用 Codex/Claude Code/Bub 已验证过的 fail-open 契约:

| 情形 | 行为 |
| --- | --- |
| `prepare_context` 抛出 `TransportError` / `ServerResponseError` / `InvalidResponseError` | 召回节点返回空更新(`{}`),不注入,不抛出 |
| `PreparedContext.status == "empty"` | 不注入,视为正常情形而非错误 |
| 显式工具(`search`/`remember`)调用失败 | 以字符串形式把可读错误返回给模型,不中断图执行 |
| 轨迹捕获失败 | 记录日志,不影响已经产出的最终 state |
| PowerContext Server 完全不可达 | 图照常运行,只是没有记忆增强,等价于未接入 PowerContext |

不引入自定义重试/熔断策略——`PowerContextClient` 本身没有重试语义,适配包也不加,重试策略留给使用方在自己的部署里通过 httpx 传输层配置。

**与 #1213 约束的对照**:

| #1213 约束 | 本提案的对应设计 |
| --- | --- |
| 复用现有接口 | 只调用 `PowerContextClient` 已公开的方法,不新增 HTTP 端点 |
| 不复刻 Runtime/Memory 行为 | 明确放弃"实现 `BaseStore`"这条看起来更原生但会复刻版本/检索语义的路径;工具函数只是请求转发 |
| 不依赖不受支持的上游扩展机制 | `StateGraph`/`add_node`/`MessagesState`/`langchain_core.tools.tool`/`RunnableConfig` 均为核实过的公开 API;明确避开已标注 deprecated 的 `create_react_agent` 作为主推荐路径 |
| 安装器需报告文件/权限/回滚 | 不适用:普通 pip/uv 依赖,没有主机侧安装器 |

## Alternatives considered

**实现 LangGraph 的 `BaseStore` 接口去接入 PowerContext。** `libs/checkpoint/langgraph/store/base/__init__.py` 把 `BaseStore` 定义为一个通用的分层命名空间键值存储,核心操作是 `Item`/`SearchItem` 的 get/put/search/list,可选支持向量检索,官方文档称其"跨 thread/跨会话的长期记忆",语义上与 PowerContext Memory 最接近,表面上看似乎是最"原生"的接入方式。但仔细对照后发现三个结构性冲突:

1. **版本与引用模型不同。** PowerContext 的 Memory 写操作(`RememberMemoryRequest`/`ReviseMemoryEntryRequest`)基于 `expected_revision` 的乐观并发控制,每次修改都产生一个新的、可追溯的 Revision;而 `BaseStore.put` 是"覆盖写"语义,没有版本概念。把前者塞进后者,要么丢失版本信息,要么在适配层里悄悄重新发明一套版本机制——这正是 issue 里明确禁止的"复刻 Memory 行为"。
2. **搜索语义不同。** `search_memory` 返回的是"匹配方式"(`matched_by`)和分数,背后是 Server 端可配置的检索策略(全文/向量/混合);`BaseStore.search` 的向量检索由 `Embeddings`/`EmbeddingsFunc` 在客户端一侧配置。如果适配层要满足 `BaseStore` 接口,就必须在客户端重新实现或者假装支持向量检索,而真正的检索仍然发生在 PowerContext Server,"谁负责检索质量"会变得模糊。
3. **`prepare_context` 无法映射。** `BaseStore` 没有"为一次模型调用准备一段有界、格式化好的上下文"这个操作,自动召回依然要绕开它、单独调用 `PowerContextClient.prepare_context`,`BaseStore` 实现本身就成了一个不完整、容易被误用的抽象。

**结论**:LangGraph 的 Checkpointer/Store 用于管理 LangGraph 自身的运行期状态,PowerContext 的 Memory 是一个有自己版本、检索、审核(Artifact Candidate Review)流程的独立系统,二者应该保持"编排引擎的状态存储"和"外部记忆服务"的清晰边界,不实现 `BaseStore`。

**用 `langchain-mcp-adapters` 直连 MCP,而不是手写工具。**

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "powercontext": {"transport": "http", "url": "http://127.0.0.1:8000/mcp"},
})
mcp_tools = await client.get_tools()  # 包含 search_memory / remember_memory / ... 等 MCP 工具面
```

优点是零维护、自动跟随 Server 端 MCP 工具面的变化;缺点是多一跳 HTTP+JSON-RPC、工具 schema 不受适配包控制,并且 `prepare_context` 依然不在 MCP 面里,自动召回这一段无论如何都要走"图节点直连 `PowerContextClient`"这条路。**结论**:核心包默认不引入 `langchain-mcp-adapters` 作为强依赖,而是放进 `mcp` extra,首个版本用手写工具与 Bub 保持一致。

**为已弃用的 `create_react_agent` 维护一份 `pre_model_hook=` 兼容包装。** `recall` 函数经过少量适配(把 `get_config()` 换成 `pre_model_hook` 接收的 `state`/`config` 参数)后可以直接作为 `pre_model_hook=recall` 传入,行为等价。考虑过是否要在包里正式维护这条兼容路径,**结论**:倾向于只文档化 `StateGraph` 一种接入方式、把兼容包装留给用户自己写,理由是维护面更小,已作为开放问题记录供评审讨论。

**在图内插入专门的"捕获节点"做增量捕获,而不是任务结束后一次性捕获。** 如果需要在长时间运行的图执行过程中做增量捕获,可以在 `agent` 节点之后插入一个轻量的 `capture` 节点(`workflow.add_node("capture", ...)`);这完全是 `StateGraph` 的原生能力,不依赖任何 prebuilt 快捷方式。**结论**:首个版本只做任务级捕获,图内增量捕获作为二期可选项。

## Additional context

**测试计划**:参照 `tests/e2e/test_dsh_http_chain.py` 的模式,用 `create_server_app` + `httpx.ASGITransport` 起一个内存态 PowerContext Server,无真实网络、无需 API Key。模型侧用一个固定输出的 fake chat model(`langgraph` 自身测试套件里已经在用类似的 `FakeToolCallingModel` 模式,见 `libs/prebuilt/tests/test_react_agent.py`),覆盖:

1. 召回节点在有 Prepared Context 时正确追加 `SystemMessage`,同一次 `ainvoke` 调用内不重复调用 Server;
2. `powercontext_search`/`powercontext_remember`/`powercontext_context` 工具经 `ToolNode` 调用后返回预期内容;
3. Server 关闭时,召回节点不抛异常,图整体执行成功;
4. `configurable["powercontext_scope_id"]` 缺失时给出清晰的报错,而不是静默使用一个错误的默认 scope。

用例放在 `integrations/langgraph/tests/`,并在仓库根 `tests/e2e/` 增补一个跨包最小契约用例,风格对齐 `tests/e2e/test_dsh_http_chain.py`。

**文档与分发计划**:新增 `docs/en/docs/how-to/configure-langgraph.md` + 中文版 `docs/zh/docs/how-to/configure-langgraph.md`;文档中显式提醒"`create_react_agent` 已弃用"这一生态现状,并给出 `StateGraph` 与(如果适用)`langchain.agents.create_agent` 两种接入示例;包以 `powercontext-langgraph` 名称独立发布到 PyPI,`mcp` extra 可选安装 `langchain-mcp-adapters`。

**里程碑拆分建议**:

1. PR1:`integrations/langgraph` 包骨架 + 自定义 `StateGraph` 召回节点 + 三个显式工具(`search`/`remember`/`context`) + 内存态契约测试;
2. PR2:任务级轨迹捕获 + 文档(中英双语,含 `create_react_agent` 迁移提示)+ scope 派生与其他插件的一致性测试;
3. PR3(可选):`mcp` extra 下的 `langchain-mcp-adapters` 直连示例、图内增量捕获节点示例。

**开放问题**:

- `powercontext_scope_id` 放在 `RunnableConfig["configurable"]` 而不是节点闭包参数,是为了支持多租户场景;如果目标用户绝大多数是单租户单进程部署,是否需要额外提供一个更简单的"构造时固定 scope_id"的便捷入口?
- LangGraph 生态在 2026 年仍处于 `create_react_agent → create_agent` 的迁移期,`langgraph` 主版本号也演进到当前的 `1.2.x`;适配包应锁定的最低版本区间建议为 `langgraph>=1.2,<2`,具体下限需要在实现阶段跑一遍最低版本 CI 确认。

**参考资料**:

- PowerContext:`src/powercontext/client/client.py`、`src/powercontext/server/mcp.py`、`src/powercontext/http/_generated/models.py`、`integrations/bub/`、`docs/en/docs/reference/interfaces.md`
- LangGraph(提交 `644815f9e5bc52ad8f7a5227a456227e9c3e639b`):
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/langgraph/langgraph/graph/state.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/langgraph/langgraph/graph/message.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/prebuilt/langgraph/prebuilt/tool_node.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/checkpoint/langgraph/checkpoint/base/__init__.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/checkpoint/langgraph/store/base/__init__.py>
  - <https://github.com/langchain-ai/langgraph/blob/644815f9e5bc52ad8f7a5227a456227e9c3e639b/libs/langgraph/langgraph/config.py>
- LangChain 官方迁移文档(2026-08 查阅,佐证 `create_react_agent` 弃用与 `langchain-mcp-adapters` 现状,非源码但用于交叉验证生态事实):
  - <https://docs.langchain.com/oss/python/migrate/langgraph-v1>
  - <https://github.com/langchain-ai/langchain-mcp-adapters>

## Are you willing to contribute to this feature?

- [x] Yes, I am willing to contribute code, docs, or design feedback.
