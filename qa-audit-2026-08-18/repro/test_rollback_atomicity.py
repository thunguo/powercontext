"""Critical test: does a raised exception actually roll back writes on OceanBase?"""
import asyncio
import os

from pydantic import SecretStr
from sqlalchemy import delete, insert, select

from powercontext.builtin.persistence.oceanbase import OceanBaseConfig, OceanBaseProfile
from powercontext.builtin.persistence.tables import BUILTIN_TABLES, SOURCE_JOURNAL_HEADS_TABLE

OCEANBASE_URL = os.environ["POWERCONTEXT_TEST_OCEANBASE_URL"]
SCOPE = "rollback-atomicity-test"


class _BoomError(Exception):
    pass


async def main():
    async with OceanBaseProfile.open(OceanBaseConfig(url=SecretStr(OCEANBASE_URL)), tables=BUILTIN_TABLES) as profile:
        db = profile.database
        async with db.transaction() as conn:
            await conn.execute(delete(SOURCE_JOURNAL_HEADS_TABLE).where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE))

        try:
            async with db.transaction() as conn:
                await conn.execute(insert(SOURCE_JOURNAL_HEADS_TABLE).values(scope_id=SCOPE, position=42))
                print("inserted row inside transaction, now raising to force rollback...")
                raise _BoomError("simulated failure after write, before commit")
        except _BoomError:
            print("caught expected exception; transaction should have rolled back")

        async with db.transaction() as conn:
            row = await conn.execute(
                select(SOURCE_JOURNAL_HEADS_TABLE.c.position).where(SOURCE_JOURNAL_HEADS_TABLE.c.scope_id == SCOPE)
            )
            value = row.scalar()
            print("value visible after rollback attempt:", value)
            if value is None:
                print("RESULT: rollback worked correctly (row absent)")
            else:
                print("RESULT: BUG! row is present despite rollback -- write was auto-committed")


asyncio.run(main())
