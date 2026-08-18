"""Control test: does rollback work correctly for the SQLite backend?

Run with: uv run python test_rollback_atomicity_sqlite.py
"""
import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import delete, insert, select

from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import BUILTIN_TABLES, SOURCE_JOURNAL_HEADS_TABLE

SCOPE = "rollback-atomicity-test"


class _BoomError(Exception):
    pass


async def main():
    db_path = Path(tempfile.gettempdir()) / "pc_rollback_control.db"
    db_path.unlink(missing_ok=True)
    async with SQLiteProfile.open(SQLiteConfig(url=f"sqlite+aiosqlite:///{db_path}"), tables=BUILTIN_TABLES) as profile:
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
                print("RESULT: rollback worked correctly (row absent) -- SQLite is NOT affected")
            else:
                print("RESULT: BUG! row is present despite rollback")


asyncio.run(main())
