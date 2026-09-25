"""Round/run ids unique to this pytest process.

Round recovery (scripts/round_recovery.py) stops every live process of this user whose
environment carries the round's NEKAISE_RUN_ID. A test that tags children with a fixed id, or
recovers a fixed id, could therefore kill the children of another test execution running at the
same time — including a live round's pytest gate. Every id that reaches a child environment, a
round snapshot or a recovery call is made unique per pytest process with rid()."""
import uuid

SUFFIX = uuid.uuid4().hex[:10]


def rid(name: str) -> str:
    """`name` made unique to this test execution (the same name always gives the same id)."""
    return f"{name}-{SUFFIX}"
