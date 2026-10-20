"""Smoke test the durable Lambda session algorithm without AWS credentials.

Run from ``tau2_retail/`` after the normal tau2 setup:
``../.venv/bin/python test_lambda_runtime.py``.
"""

from lambda_runtime.handler import handle_call
from lambda_runtime.store import MemoryStore


def call(store: MemoryStore, tool: str, arguments: dict, session: str, call_id: str):
    return handle_call(
        run_id="test-run",
        tool_name=tool,
        arguments=arguments,
        session_id=session,
        call_id=call_id,
        store=store,
    )


def main() -> None:
    store = MemoryStore()
    order_id = "#W4776164"
    assert call(store, "get_order_details", {"order_id": order_id}, "a", "read-1")[1][
        "status"
    ] == "pending"

    first = call(
        store,
        "cancel_pending_order",
        {"order_id": order_id, "reason": "no longer needed"},
        "a",
        "write-1",
    )
    replay = call(
        store,
        "cancel_pending_order",
        {"order_id": order_id, "reason": "no longer needed"},
        "a",
        "write-1",
    )
    assert first == replay
    assert call(store, "get_order_details", {"order_id": order_id}, "a", "read-2")[1][
        "status"
    ] == "cancelled"
    assert call(store, "get_order_details", {"order_id": order_id}, "b", "read-3")[1][
        "status"
    ] == "pending"
    print("Lambda state, session isolation, and call-id replay: ok")


if __name__ == "__main__":
    main()
