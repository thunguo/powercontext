"""Reproduce OceanBaseMemoryFTSIndex.initialize crash under concurrent startup."""
from __future__ import annotations

import asyncio
import os

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    _OCEANBASE_FTS_INDEX_NAME,
)
from powercontext.builtin.persistence.oceanbase.profile import _register_official_dialect
from powercontext.builtin.persistence.schema import create_tables
from powercontext.builtin.persistence.tables import BUILTIN_TABLES

OCEANBASE_URL = os.environ["POWERCONTEXT_TEST_OCEANBASE_URL"]
DROP_SQL = f"DROP INDEX {_OCEANBASE_FTS_INDEX_NAME} ON pc_memory_entry_heads"


async def worker(worker_id: int) -> str:
    _register_official_dialect()
    engine = create_async_engine(OCEANBASE_URL)
    index = OceanBaseMemoryFTSIndex()
    try:
        async with engine.begin() as conn:
            await create_tables(conn, BUILTIN_TABLES)
            await asyncio.sleep(0.05)
            await index.initialize(conn)
        return f"worker {worker_id}: ok"
    except Exception as exc:
        return f"worker {worker_id}: {type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()


async def main() -> None:
    _register_official_dialect()
    engine = create_async_engine(OCEANBASE_URL)
    async with engine.begin() as conn:
        await create_tables(conn, BUILTIN_TABLES)
        try:
            await conn.exec_driver_sql(DROP_SQL)
        except Exception:
            pass
    await engine.dispose()

    results = await asyncio.gather(*(worker(i) for i in range(4)))
    for line in results:
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
