"""Reproduce confirmed PowerContext defects against a live local Server.

Run from the repository root:

    DEEPSEEK_API_KEY=... \\
    POWERCONTEXT_TEST_OCEANBASE_URL='mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4' \\
    uv run python scripts/issue_audit/repro.py

The script never prints secret values. It writes a JSON report to
``scripts/issue_audit/results.json``.
"""

# ruff: noqa: RUF001, S108, S310, SIM117

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

REPORT_PATH = Path(__file__).resolve().parent / "results.json"
DEEPSEEK_MODEL = "deepseek:deepseek-v4-flash"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"


@dataclass
class Finding:
    id: str
    title: str
    confirmed: bool
    severity: str
    evidence: dict[str, Any]
    error: str | None = None


def _record(findings: list[Finding], finding: Finding) -> None:
    findings.append(finding)
    status = "CONFIRMED" if finding.confirmed else "NOT_CONFIRMED"
    print(f"[{status}] {finding.id}: {finding.title}", flush=True)
    if finding.error:
        print(f"    error: {finding.error}", flush=True)


def finding_cli_root_json_ignored_by_doctor() -> Finding:
    from powercontext.cli.app import create_cli
    from powercontext.cli.system import doctor_app

    cli = create_cli([doctor_app])
    root = CliRunner().invoke(cli, ["--json", "doctor"])
    nested = CliRunner().invoke(cli, ["doctor", "--json"])
    root_is_json = False
    nested_is_json = False
    try:
        json.loads(root.output)
        root_is_json = True
    except json.JSONDecodeError:
        pass
    try:
        json.loads(nested.output)
        nested_is_json = True
    except json.JSONDecodeError:
        pass
    return Finding(
        id="cli-root-json-ignored-by-doctor",
        title="Root `--json` is ignored by `doctor`; only `doctor --json` emits JSON",
        confirmed=nested_is_json and not root_is_json,
        severity="medium",
        evidence={
            "root_command": "powercontext --json doctor",
            "nested_command": "powercontext doctor --json",
            "root_exit_code": root.exit_code,
            "nested_exit_code": nested.exit_code,
            "root_output_head": root.output[:240],
            "nested_output_head": nested.output[:240],
            "root_is_json": root_is_json,
            "nested_is_json": nested_is_json,
        },
    )


def _http_status(url: str) -> tuple[int, str]:
    request = Request(url, method="GET", headers={"User-Agent": "powercontext-issue-audit"})
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.geturl()
    except HTTPError as error:
        return error.code, url


def finding_cli_docs_url_points_at_main() -> Finding:
    from powercontext.cli.app import DOCUMENTATION_URL, ISSUES_URL

    docs_status, docs_url = _http_status(DOCUMENTATION_URL)
    issues_status, _ = _http_status(ISSUES_URL)
    master_url = DOCUMENTATION_URL.replace("/tree/main/", "/tree/master/")
    master_status, master_final = _http_status(master_url)
    return Finding(
        id="cli-docs-url-main-404",
        title="CLI epilog documents `/tree/main/` while the default branch is `master`",
        confirmed="/tree/main/" in DOCUMENTATION_URL and docs_status >= 400 and master_status < 400,
        severity="low",
        evidence={
            "documentation_url": DOCUMENTATION_URL,
            "documentation_final_url": docs_url,
            "documentation_status": docs_status,
            "master_url": master_url,
            "master_final_url": master_final,
            "master_status": master_status,
            "issues_url": ISSUES_URL,
            "issues_status": issues_status,
        },
    )


def _asgi_client(tmp_db: Path, **settings_kwargs: Any) -> tuple[Any, TestClient]:
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.server.factory import create_server_app
    from powercontext.server.settings import ServerSettings

    settings = ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_db}"),
        **settings_kwargs,
    )
    app = create_server_app(settings=settings)
    return app, TestClient(app)


def finding_http_surface(tmp_path: Path) -> Finding:
    _app, client = _asgi_client(tmp_path / "http-surface.db")
    with client:
        extract = client.post("/v1/memory/mem-1/extract", json={"scope_id": "demo"})
        unknown = client.get("/v1/does-not-exist")
        flush = client.post("/v1/memory/flush", json={"scope_id": "demo"})
        openapi_root = client.get("/openapi.json")
        openapi_v1 = client.get("/v1/openapi.json")
        docs = client.get("/docs")
        redoc = client.get("/redoc")
        schema = openapi_root.json()
        contract_paths = set(schema.get("paths", {}))
    return Finding(
        id="http-contract-surface",
        title="Memory extract path 404s; FastAPI docs live outside the published contract",
        confirmed=(
            extract.status_code == 404
            and unknown.json().get("detail") == "Not Found"
            and "error" not in unknown.json()
            and flush.status_code == 200
            and openapi_v1.status_code == 404
            and docs.status_code == 200
            and "/docs" not in contract_paths
            and "/v1/memory/{memory_id}/extract" not in contract_paths
        ),
        severity="medium",
        evidence={
            "extract_status": extract.status_code,
            "extract_body": extract.json()
            if extract.headers.get("content-type", "").startswith("application/json")
            else extract.text[:300],
            "unknown_route_body": unknown.json(),
            "unknown_uses_contract_error_envelope": "error" in unknown.json(),
            "flush_status": flush.status_code,
            "openapi_json_status": openapi_root.status_code,
            "openapi_version": schema.get("openapi"),
            "v1_openapi_json_status": openapi_v1.status_code,
            "docs_status": docs.status_code,
            "redoc_status": redoc.status_code,
            "contract_contains_docs": "/docs" in contract_paths,
            "contract_contains_extract": "/v1/memory/{memory_id}/extract" in contract_paths,
            "sample_contract_paths": sorted(path for path in contract_paths if path.startswith("/v1/memory")),
        },
    )


def finding_mcp_initialize_and_tools(tmp_path: Path) -> Finding:
    _app, client = _asgi_client(tmp_path / "mcp.db")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with client:
        missing_version = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"capabilities": {}, "clientInfo": {"name": "repro", "version": "0"}},
            },
        )
        with_version = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "repro", "version": "0"},
                },
            },
        )
        session = with_version.headers.get("mcp-session-id")
        initialized = client.post(
            "/mcp",
            headers={**headers, **({"mcp-session-id": session} if session else {})},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools = client.post(
            "/mcp",
            headers={**headers, **({"mcp-session-id": session} if session else {})},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        missing_arg = client.post(
            "/mcp",
            headers={**headers, **({"mcp-session-id": session} if session else {})},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "search_memory", "arguments": {}},
            },
        )

    def _jsonrpc(response: httpx.Response | Any) -> dict[str, Any]:
        text = response.text
        if text.startswith("event:"):
            for line in text.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        try:
            return response.json()
        except Exception:
            return {"raw": text[:500], "status": response.status_code}

    missing_body = _jsonrpc(missing_version)
    version_body = _jsonrpc(with_version)
    tools_body = _jsonrpc(tools)
    missing_arg_body = _jsonrpc(missing_arg)
    tool_names = [item.get("name") for item in tools_body.get("result", {}).get("tools", [])]
    result = version_body.get("result") or {}
    confirmed = (
        (missing_version.status_code >= 400 or "error" in missing_body)
        and "flush_memory" not in tool_names
        and "prepare_context" not in tool_names
    )
    return Finding(
        id="mcp-initialize-and-tool-surface",
        title="MCP initialize is strict about protocolVersion; agent tools omit flush/prepare/generate",
        confirmed=confirmed,
        severity="high",
        evidence={
            "missing_protocol_version_status": missing_version.status_code,
            "missing_protocol_version_body": missing_body,
            "valid_initialize_status": with_version.status_code,
            "initialize_result_keys": sorted(result) if isinstance(result, dict) else result,
            "initialize_protocol_version": result.get("protocolVersion") if isinstance(result, dict) else None,
            "initialized_notification_status": initialized.status_code,
            "tool_names": tool_names,
            "missing_agent_tools": sorted(
                {
                    "flush_memory",
                    "prepare_context",
                    "generate_experience",
                    "generate_skill",
                    "get_capabilities",
                }
                - set(tool_names)
            ),
            "search_memory_missing_args_status": missing_arg.status_code,
            "search_memory_missing_args_body": missing_arg_body,
        },
    )


def finding_unmapped_httpx_timeout() -> Finding:
    from powercontext.builtin.inference.pydantic_ai import _map_error

    mapped = _map_error(httpx.TimeoutException("connect timed out"), operation="generate", timeout_seconds=30.0)
    return Finding(
        id="inference-unmapped-httpx-timeout",
        title="httpx.TimeoutException is not mapped to InferenceTimeoutError",
        confirmed=mapped is None,
        severity="medium",
        evidence={"mapped": None if mapped is None else type(mapped).__name__},
    )


def finding_default_projector_omits_content() -> Finding:
    from powercontext import SourceMaterialization
    from powercontext.builtin.artifacts.memory.extraction import DefaultMemoryEvidenceProjector
    from powercontext.builtin.runtime.composition import _ContentEvidenceProjector
    from powercontext.builtin.sources import ContentSource

    source = ContentSource(
        name="note-1",
        materialization=SourceMaterialization.CAPTURED,
        content="The project selected OceanBase because of HTAP.",
    )
    default = DefaultMemoryEvidenceProjector().project_source(source)
    runtime = _ContentEvidenceProjector().project_source(source)
    return Finding(
        id="default-memory-projector-omits-content",
        title="Default Memory evidence projector hides ContentSource.content; only the Runtime projector includes it",
        confirmed="content" not in default and runtime.get("content") == source.content,
        severity="medium",
        evidence={"default_projection": default, "runtime_projection": runtime},
    )


def finding_readiness_probe_budget() -> Finding:
    from powercontext.builtin.runtime.config import InferenceConfig
    from powercontext.builtin.runtime.readiness import READINESS_PROBE_TIMEOUT_SECONDS

    config = InferenceConfig()
    return Finding(
        id="readiness-probe-two-second-budget",
        title="Generation readiness probe is hard-coded to 2s, independent of generation_timeout_seconds",
        confirmed=READINESS_PROBE_TIMEOUT_SECONDS == 2.0 and config.generation_timeout_seconds == 30.0,
        severity="high",
        evidence={
            "readiness_probe_timeout_seconds": READINESS_PROBE_TIMEOUT_SECONDS,
            "default_generation_timeout_seconds": config.generation_timeout_seconds,
        },
    )


def finding_sqlite_cjk_and_short_token_fts(tmp_path: Path) -> Finding:
    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        RememberMemoryRequest,
        SearchMemoryRequest,
        open_builtin_runtime,
    )

    async def scenario() -> dict[str, Any]:
        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'fts.db'}")
        async with open_builtin_runtime(BuiltinConfig(database=database)) as runtime:
            memory = runtime.memory.for_scope("fts-repro")
            await memory.remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(kind="decision", text="项目选择 OceanBase，因为需要 HTAP。"),
                        MemoryEntryInput(kind="preference", text="Use uv for dependency management."),
                    )
                )
            )
            cjk = await memory.search(SearchMemoryRequest(query="OceanBase HTAP", mode="fts"))
            short = await memory.search(SearchMemoryRequest(query="uv", mode="fts"))
            return {
                "cjk_texts": [hit.text for hit in cjk.hits],
                "short_texts": [hit.text for hit in short.hits],
                "cjk_mode": cjk.mode,
                "short_mode": short.mode,
            }

    result = asyncio.run(scenario())
    return Finding(
        id="sqlite-fts-cjk-and-short-token",
        title="SQLite Analyzer v1 FTS can recall CJK and the short token `uv`",
        confirmed=any("OceanBase" in text for text in result["cjk_texts"])
        and any("uv" in text for text in result["short_texts"]),
        severity="info",
        evidence=result,
    )


def finding_deepseek_api_and_structured_generation() -> Finding:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return Finding(
            id="deepseek-v4-flash-generation",
            title="DeepSeek V4 Flash structured Memory extraction",
            confirmed=False,
            severity="high",
            evidence={"skipped": True, "reason": "DEEPSEEK_API_KEY is unset"},
        )

    started = time.perf_counter()
    raw = httpx.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Reply with the single token OK."}],
            "max_tokens": 16,
        },
        timeout=60.0,
    )
    raw_latency_ms = (time.perf_counter() - started) * 1000
    raw_body = raw.json()
    raw_model = (
        (raw_body.get("model") or raw_body.get("choices", [{}])[0].get("model")) if isinstance(raw_body, dict) else None
    )

    from pydantic_ai.models import infer_model

    from powercontext import SourceMaterialization
    from powercontext.builtin.artifacts.memory import (
        LLMMemoryCandidatePipeline,
        MemoryCandidateRequest,
        MemoryExtractionInput,
        MemoryExtractionOutput,
        memory_extraction_instructions,
    )
    from powercontext.builtin.artifacts.memory.prompts import MemoryExtractionProfile
    from powercontext.builtin.inference.pydantic_ai import InferenceLimits, PydanticAIStructuredGenerator
    from powercontext.builtin.runtime.composition import _ContentEvidenceProjector
    from powercontext.builtin.sources import ContentSource

    async def extract() -> dict[str, Any]:
        os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
        model = await infer_model(DEEPSEEK_MODEL).__aenter__()
        try:
            generator = PydanticAIStructuredGenerator(
                model=model,
                instructions=memory_extraction_instructions(MemoryExtractionProfile.CODING),
                input_type=MemoryExtractionInput,
                output_type=MemoryExtractionOutput,
                limits=InferenceLimits(timeout_seconds=90.0, max_requests=2),
                name="issue_audit_memory_extraction",
            )
            pipeline = LLMMemoryCandidatePipeline(generator, evidence_projector=_ContentEvidenceProjector())
            source = ContentSource(
                name="decision-1",
                materialization=SourceMaterialization.CAPTURED,
                content="The team selected OceanBase because it provides HTAP for mixed OLTP and analytics.",
            )
            started_extract = time.perf_counter()
            try:
                candidates = await pipeline.extract(
                    MemoryCandidateRequest(sources=(source,), artifacts=(), current_entries=())
                )
                extract_error = None
            except Exception as error:
                candidates = ()
                extract_error = f"{type(error).__name__}: {error}"
            extract_latency_ms = (time.perf_counter() - started_extract) * 1000

            probe_started = time.perf_counter()
            from powercontext.builtin.inference.pydantic_ai import probe_pydantic_ai_model

            probe_error = None
            try:
                await probe_pydantic_ai_model(model, timeout_seconds=2.0)
                probe_ok = True
            except Exception as error:
                probe_ok = False
                probe_error = f"{type(error).__name__}: {error}"
            probe_latency_ms = (time.perf_counter() - probe_started) * 1000
            return {
                "candidate_count": len(candidates),
                "candidate_texts": [candidate.text for candidate in candidates],
                "extract_error": extract_error,
                "extract_latency_ms": round(extract_latency_ms, 1),
                "probe_ok_under_2s": probe_ok,
                "probe_error": probe_error,
                "probe_latency_ms": round(probe_latency_ms, 1),
            }
        finally:
            await model.__aexit__(None, None, None)

    structured = asyncio.run(extract())
    defect = structured["extract_error"] is not None or not structured["probe_ok_under_2s"]
    return Finding(
        id="deepseek-v4-flash-generation",
        title="DeepSeek V4 Flash 0731 generation against PowerContext structured extraction and 2s readiness probe",
        confirmed=defect,
        severity="high",
        evidence={
            "raw_http_status": raw.status_code,
            "raw_latency_ms": round(raw_latency_ms, 1),
            "raw_model": raw_model or raw_body.get("model") if isinstance(raw_body, dict) else None,
            "raw_error": None if raw.status_code == 200 else raw_body,
            "structured": structured,
            "model_id": DEEPSEEK_MODEL,
        },
    )


def finding_oceanbase_vector() -> Finding:
    url = os.environ.get("POWERCONTEXT_TEST_OCEANBASE_URL")
    if not url:
        return Finding(
            id="oceanbase-vector",
            title="OceanBase vector index initialize with a unit-normalized L2 profile",
            confirmed=False,
            severity="high",
            evidence={"skipped": True, "reason": "POWERCONTEXT_TEST_OCEANBASE_URL is unset"},
        )

    from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
    from powercontext.builtin.inference import EmbeddingResult
    from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        RememberMemoryRequest,
        SearchMemoryRequest,
        open_builtin_runtime,
    )

    profile = EmbeddingProfile(
        profile_id="issue-audit-ob-vector-v1",
        model="test",
        dimension=3,
        distance="l2",
        normalization="unit",
    )

    class _KeywordEmbeddingModel:
        def __init__(self) -> None:
            self.profile = profile

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            vectors = tuple((1.0, 0.0, 0.0) if "oceanbase" in text.casefold() else (0.0, 1.0, 0.0) for text in texts)
            return EmbeddingResult(vectors=vectors)

    async def scenario() -> dict[str, Any]:
        async with open_builtin_runtime(
            BuiltinConfig(database=OceanBaseConfig(url=SecretStr(url))),
            embedding_model=_KeywordEmbeddingModel(),
        ) as runtime:
            memory = runtime.memory.for_scope(f"ob-vector-{int(time.time())}")
            await memory.remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="decision", text="OceanBase HTAP decision."),))
            )
            result = await memory.search(SearchMemoryRequest(query="OceanBase", mode="vector"))
            capabilities = await runtime.capabilities()
            return {
                "mode": result.mode,
                "texts": [hit.text for hit in result.hits],
                "matched_by": [list(hit.matched_by) for hit in result.hits],
                "memory_search_modes": list(capabilities.memory_search_modes),
            }

    try:
        evidence = asyncio.run(scenario())
        return Finding(
            id="oceanbase-vector",
            title="OceanBase vector search with a synthetic embedding profile",
            confirmed=False,
            severity="info",
            evidence=evidence,
        )
    except Exception as error:
        return Finding(
            id="oceanbase-vector",
            title="OceanBase vector index initialize or search fails on the local CE slim tenant",
            confirmed=True,
            severity="high",
            evidence={"exception_type": type(error).__name__, "exception": str(error)},
            error=traceback.format_exc(),
        )


def finding_oceanbase() -> Finding:
    url = os.environ.get("POWERCONTEXT_TEST_OCEANBASE_URL")
    if not url:
        return Finding(
            id="oceanbase-live",
            title="OceanBase MySQL-mode persistence, FTS, and schema smoke",
            confirmed=False,
            severity="high",
            evidence={"skipped": True, "reason": "POWERCONTEXT_TEST_OCEANBASE_URL is unset"},
        )

    from sqlalchemy import select, text

    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.persistence.oceanbase import OceanBaseConfig, OceanBaseProfile
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        RememberMemoryRequest,
        SearchMemoryRequest,
        open_builtin_runtime,
    )

    async def scenario() -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        async with OceanBaseProfile.open(OceanBaseConfig(url=SecretStr(url)), tables=()) as profile:
            async with profile.database.transaction() as connection:
                evidence["select_1"] = await connection.scalar(select(1))
                mode = (await connection.exec_driver_sql("SHOW VARIABLES LIKE 'ob_compatibility_mode'")).first()
                evidence["compatibility_mode"] = None if mode is None else list(mode)
                charset = (await connection.exec_driver_sql("SHOW VARIABLES LIKE 'character_set_database'")).first()
                evidence["character_set_database"] = None if charset is None else list(charset)

        async with open_builtin_runtime(BuiltinConfig(database=OceanBaseConfig(url=SecretStr(url)))) as runtime:
            scope = f"ob-repro-{int(time.time())}"
            memory = runtime.memory.for_scope(scope)
            remembered = await memory.remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(kind="decision", text="项目选择 OceanBase，因为需要 HTAP。"),
                        MemoryEntryInput(kind="preference", text="Use uv for dependency management."),
                    )
                )
            )
            cjk = await memory.search(SearchMemoryRequest(query="OceanBase HTAP", mode="fts"))
            short = await memory.search(SearchMemoryRequest(query="uv", mode="fts"))
            english = await memory.search(SearchMemoryRequest(query="dependency management", mode="fts"))
            evidence["memory_ref"] = remembered.memory_ref.model_dump() if remembered.memory_ref else None
            evidence["cjk_texts"] = [hit.text for hit in cjk.hits]
            evidence["short_token_uv_texts"] = [hit.text for hit in short.hits]
            evidence["english_texts"] = [hit.text for hit in english.hits]
            capabilities = await runtime.capabilities()
            evidence["capabilities"] = capabilities.model_dump()
            async with runtime._provider.database.transaction() as connection:
                fts = await connection.scalar(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.statistics
                        WHERE table_schema = DATABASE()
                          AND table_name = 'pc_memory_entry_heads'
                          AND index_name = 'ix_pc_memory_entry_heads_fts'
                        """
                    )
                )
                evidence["fts_index_present"] = int(fts or 0)
        evidence["cjk_hit"] = any("OceanBase" in text for text in evidence["cjk_texts"])
        evidence["uv_hit"] = any("uv" in text for text in evidence["short_token_uv_texts"])
        evidence["english_hit"] = any("dependency" in text.lower() for text in evidence["english_texts"])
        return evidence

    try:
        evidence = asyncio.run(scenario())
    except Exception as error:
        return Finding(
            id="oceanbase-live",
            title="OceanBase MySQL-mode persistence, FTS, and schema smoke",
            confirmed=True,
            severity="high",
            evidence={"exception_type": type(error).__name__, "exception": str(error)},
            error=traceback.format_exc(),
        )

    defect = bool(evidence.get("select_1") == 1 and (not evidence.get("cjk_hit") or not evidence.get("uv_hit")))
    return Finding(
        id="oceanbase-live",
        title="OceanBase MySQL-mode persistence works, but SPACE FTS may miss CJK/short tokens that SQLite recalls",
        confirmed=defect,
        severity="high",
        evidence=evidence,
    )


def finding_deepseek_server_readiness(tmp_path: Path) -> Finding:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return Finding(
            id="deepseek-server-readiness",
            title="Server readiness with DeepSeek V4 Flash becomes degraded under the 2s probe",
            confirmed=False,
            severity="high",
            evidence={"skipped": True, "reason": "DEEPSEEK_API_KEY is unset"},
        )

    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime.config import InferenceConfig
    from powercontext.server.factory import create_server_app
    from powercontext.server.settings import ServerSettings

    os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
    settings = ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'deepseek-server.db'}"),
        inference=InferenceConfig(
            generation_model=DEEPSEEK_MODEL,
            generation_timeout_seconds=90.0,
        ),
    )
    app = create_server_app(settings=settings)
    started = time.perf_counter()
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        caps = client.get("/v1/capabilities")
    latency_ms = (time.perf_counter() - started) * 1000
    body = ready.json()
    return Finding(
        id="deepseek-server-readiness",
        title="Server `/health/ready` reports degraded/not_ready for a working DeepSeek V4 Flash model because the probe budget is 2s",
        confirmed=ready.status_code in {200, 503} and body.get("status") in {"degraded", "not_ready"},
        severity="high",
        evidence={
            "ready_status_code": ready.status_code,
            "ready_body": body,
            "capabilities": caps.json(),
            "startup_and_ready_latency_ms": round(latency_ms, 1),
        },
    )


def main() -> int:
    tmp_path = Path("/tmp/powercontext-issue-audit")
    tmp_path.mkdir(parents=True, exist_ok=True)
    findings: list[Finding] = []
    runners = [
        finding_cli_root_json_ignored_by_doctor,
        finding_cli_docs_url_points_at_main,
        lambda: finding_http_surface(tmp_path),
        lambda: finding_mcp_initialize_and_tools(tmp_path),
        finding_unmapped_httpx_timeout,
        finding_default_projector_omits_content,
        finding_readiness_probe_budget,
        lambda: finding_sqlite_cjk_and_short_token_fts(tmp_path),
        finding_deepseek_api_and_structured_generation,
        lambda: finding_deepseek_server_readiness(tmp_path),
        lambda: finding_oceanbase(),
        finding_oceanbase_vector,
    ]
    for runner in runners:
        try:
            _record(findings, runner())
        except Exception as error:
            _record(
                findings,
                Finding(
                    id=getattr(runner, "__name__", "unknown"),
                    title="reproduction raised unexpectedly",
                    confirmed=False,
                    severity="high",
                    evidence={"exception_type": type(error).__name__, "exception": str(error)},
                    error=traceback.format_exc(),
                ),
            )

    payload = {
        "model": DEEPSEEK_MODEL,
        "oceanbase_url_configured": bool(os.environ.get("POWERCONTEXT_TEST_OCEANBASE_URL")),
        "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "findings": [asdict(item) for item in findings],
        "confirmed_count": sum(1 for item in findings if item.confirmed),
    }
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {REPORT_PATH} confirmed={payload['confirmed_count']}/{len(findings)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
