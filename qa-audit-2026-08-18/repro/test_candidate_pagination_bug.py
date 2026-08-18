"""Confirm: keyset pagination on random candidate_id can permanently skip
a candidate proposed between two page fetches (Review Inbox bug)."""
import asyncio

from pydantic import BaseModel

from powercontext.builtin.persistence.candidates import CandidateRepository
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.tables import BUILTIN_TABLES
from powercontext.builtin.review.models import CandidateStatus
from powercontext.sources import SourceRef

_EVIDENCE = (SourceRef(source_type="content", source_id="task-1"),)


class _Proposal(BaseModel):
    note: str


async def main() -> None:
    repo = CandidateRepository({"note": _Proposal})
    async with SQLiteProfile.open(SQLiteConfig(), tables=BUILTIN_TABLES) as profile:
        async with profile.database.transaction() as conn:
            # Two candidates proposed "first", with IDs that happen to sort
            # as most random UUID hex strings would relative to each other.
            await repo.create(
                conn, "scope-1", "cand_aaaaaaaa", "note", _Proposal(note="first"),
                sources=_EVIDENCE, artifacts=(), target=None, reason=None,
            )
            await repo.create(
                conn, "scope-1", "cand_cccccccc", "note", _Proposal(note="second"),
                sources=_EVIDENCE, artifacts=(), target=None, reason=None,
            )

            page1 = await repo.list(
                conn, "scope-1", status=CandidateStatus.PENDING, family=None, cursor=None, limit=1,
            )
            print("page1 candidate_ids:", [c.candidate_id for c in page1.candidates], "next_cursor:", page1.next_cursor)
            assert [c.candidate_id for c in page1.candidates] == ["cand_aaaaaaaa"]
            assert page1.next_cursor == "cand_aaaaaaaa"

            # A third candidate is proposed by another concurrent agent/tool call
            # AFTER the reviewer fetched page 1, but its random ID sorts BEFORE
            # the cursor the reviewer is now paging from.
            await repo.create(
                conn, "scope-1", "cand_00000000", "note", _Proposal(note="third, proposed after page1 was read"),
                sources=_EVIDENCE, artifacts=(), target=None, reason=None,
            )

            page2 = await repo.list(
                conn, "scope-1", status=CandidateStatus.PENDING, family=None,
                cursor=page1.next_cursor, limit=10,
            )
            print("page2 candidate_ids:", [c.candidate_id for c in page2.candidates])

            seen = {c.candidate_id for c in page1.candidates} | {c.candidate_id for c in page2.candidates}
            print("all candidate_ids seen across full pagination traversal:", seen)
            if "cand_00000000" not in seen:
                print("BUG CONFIRMED: 'cand_00000000' was silently dropped from the Review Inbox traversal")
            else:
                print("not reproduced: candidate was found")


asyncio.run(main())
