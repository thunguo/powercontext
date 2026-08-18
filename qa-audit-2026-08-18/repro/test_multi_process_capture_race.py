"""Simulate two independent PowerContext server processes (e.g. horizontally
scaled replicas) sharing one OceanBase database, both capturing Sources into
the same scope concurrently. Each 'process' is modeled as its own
open_builtin_runtime() instance, so the in-process per-scope asyncio.Lock in
_RelationalSources does NOT protect across them -- only OceanBase's
transaction/locking guarantees would. Bug #1 breaks those guarantees."""
import asyncio
import os
from uuid import uuid4

from pydantic import SecretStr

from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, open_builtin_runtime

OCEANBASE_URL = os.environ["POWERCONTEXT_TEST_OCEANBASE_URL"]


async def run_process(process_id: int, scope_id: str, worker_ids: range) -> list[str]:
    config = BuiltinConfig(database=OceanBaseConfig(url=SecretStr(OCEANBASE_URL)))
    async with open_builtin_runtime(config) as runtime:
        sources = runtime.sources.for_scope(scope_id)

        async def capture(i: int) -> str:
            try:
                receipt = await sources.capture(
                    CaptureSource(source_id=f"p{process_id}-task-{i}", content=f"payload {i}", metadata={})
                )
                return f"process {process_id} worker {i}: ok sequence={receipt.sequence}"
            except Exception as exc:
                return f"process {process_id} worker {i}: {type(exc).__name__}: {exc}"

        return await asyncio.gather(*(capture(i) for i in worker_ids))


async def main() -> None:
    scope_id = f"multi-process-capture-{uuid4()}"
    # Two independent runtime instances (independent lock dictionaries),
    # each issuing several concurrent captures into the SAME scope.
    results_a, results_b = await asyncio.gather(
        run_process(1, scope_id, range(5)),
        run_process(2, scope_id, range(5)),
    )
    all_results = results_a + results_b
    for line in all_results:
        print(line)
    failures = [r for r in all_results if "ok" not in r]
    print(f"\n{len(failures)}/{len(all_results)} captures failed across two independent runtime processes sharing one scope")


asyncio.run(main())
