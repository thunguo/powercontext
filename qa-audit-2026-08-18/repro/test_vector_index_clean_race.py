"""Clean-slate concurrent OceanBase vector index creation race."""
from __future__ import annotations

import asyncio
import os

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.oceanbase.profile import _register_official_dialect
from powercontext.builtin.persistence.schema import create_tables
from powercontext.builtin.persistence.tables import BUILTIN_TABLES

OCEANBASE_URL = os.environ["POWERCONTEXT_TEST_OCEANBASE_URL"]
PROFILE = EmbeddingProfile(
    profile_id="race-test",
    model="test",
    dimension=3,
    distance="l2",
    normalization="unit",
)
DROP_SQL = "DROP INDEX ix_pc_memory_vector_entries_embedding ON pc_memory_vector_entries"


async def worker(worker_id: int) -> str:
    _register_official_dialect()
    engine = create_async_engine(OCEANBASE_URL)
    index = OceanBaseMemoryVectorIndex(PROFILE)
    try:
        async with engine.begin() as conn:
            await create_tables(conn, BUILTIN_TABLES + index.tables)
            await asyncio.sleep(0.1)
            await index.initialize(conn)
        return f"worker {worker_id}: ok"
    except Exception as exc:
        return f"worker {worker_id}: {type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()


async def main() -> None:
    _register_official_dialect()
    engine = create_async_engine(OCEANBASE_URL)
    index = OceanBaseMemoryVectorIndex(PROFILE)
    async with engine.begin() as conn:
        await create_tables(conn, BUILTIN_TABLES + index.tables)
        try:
            await conn.exec_driver_sql(DROP_SQL)
            print("dropped existing vector index")
        except Exception as exc:
            print(f"drop skipped: {exc}")
    await engine.dispose()

    for line in await asyncio.gather(*(worker(i) for i in range(4))):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
