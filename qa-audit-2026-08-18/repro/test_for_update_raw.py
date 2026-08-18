"""Test whether SELECT ... FOR UPDATE actually blocks concurrent transactions on OceanBase."""
import asyncio
import os
import time

from pydantic import SecretStr
from sqlalchemy import delete, insert, select, text, update

from powercontext.builtin.persistence.oceanbase import OceanBaseConfig, OceanBaseProfile
from powercontext.builtin.persistence.tables import BUILTIN_TABLES, SOURCE_JOURNAL_HEADS_TABLE

OCEANBASE_URL = os.environ["POWERCONTEXT_TEST_OCEANBASE_URL"]
SCOPE = "for-update-raw-test"


async def worker(db, worker_id, barrier):
    async with db.transaction() as conn:
        t0 = time.monotonic()
        await barrier.wait()
        # No-op update first (mirrors _lock_journal_head)
        await conn.execute(
            update(SOURCE_JOURNAL_HEADS_TABLE)
            .where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE)
            .values(position=SOURCE_JOURNAL_HEADS_TABLE.c.position)
        )
        row = await conn.execute(
            select(SOURCE_JOURNAL_HEADS_TABLE.c.position)
            .where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE)
            .with_for_update()
        )
        position = row.scalar()
        held = time.monotonic() - t0
        print(f"worker {worker_id}: acquired lock at +{held:.3f}s, saw position={position}")
        await asyncio.sleep(0.3)
        await conn.execute(
            update(SOURCE_JOURNAL_HEADS_TABLE)
            .where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE)
            .values(position=position + 1)
        )
        print(f"worker {worker_id}: committed position={position + 1}")


async def main():
    async with OceanBaseProfile.open(OceanBaseConfig(url=SecretStr(OCEANBASE_URL)), tables=BUILTIN_TABLES) as profile:
        db = profile.database
        async with db.transaction() as conn:
            await conn.execute(delete(SOURCE_JOURNAL_HEADS_TABLE).where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE))
            await conn.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id=SCOPE, position=0))
        barrier = asyncio.Barrier(3)
        await asyncio.gather(*(worker(db, i, barrier) for i in range(3)))

asyncio.run(main())
