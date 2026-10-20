"""Small, durable session records for the tau2 retail Lambda.

The complete tau2 retail database is about 2.8 MiB, which is too large for a
DynamoDB item.  Instead of storing a database snapshot, a session stores the
successful *mutating* tool calls that produced it.  Each invocation starts from
the bundled canonical database and replays those calls.  Retail's tools are
deterministic, so this reconstructs the exact store without leaking state
between sessions.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol


SESSION_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Session:
    """A versioned list of the successful writes for one run and session."""

    actions: list[dict[str, Any]]
    version: int


class SessionStore(Protocol):
    """Compare-and-swap storage for reconstructed retail state."""

    def read(self, key: str) -> Session:
        """Return the persisted session or an empty initial session."""
        ...

    def write(self, key: str, actions: list[dict[str, Any]], version: int) -> bool:
        """Save actions only if the caller read ``version``."""
        ...


class MemoryStore:
    """In-memory store used only by unit tests and local handler exercises."""

    def __init__(self) -> None:
        self._records: dict[str, Session] = {}

    def read(self, key: str) -> Session:
        session = self._records.get(key)
        if session is None:
            return Session(actions=[], version=0)
        return Session(actions=copy.deepcopy(session.actions), version=session.version)

    def write(self, key: str, actions: list[dict[str, Any]], version: int) -> bool:
        current = self._records.get(key)
        if current is not None and current.version != version:
            return False
        self._records[key] = Session(actions=copy.deepcopy(actions), version=version + 1)
        return True


class DynamoStore:
    """DynamoDB-backed sessions with a 24-hour TTL and optimistic writes."""

    def __init__(self, table_name: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        import boto3

        self._table = boto3.resource("dynamodb").Table(table_name)
        self._ttl_seconds = ttl_seconds

    def read(self, key: str) -> Session:
        item = self._table.get_item(Key={"pk": key}).get("Item")
        if item is None:
            return Session(actions=[], version=0)
        version = int(item["version"])
        if int(item["expires_at"]) <= time.time():
            # TTL deletion is asynchronous; treating an expired record as empty gives
            # the next write the old version so it safely replaces the record.
            return Session(actions=[], version=version)
        return Session(actions=json.loads(item["actions_json"]), version=version)

    def write(self, key: str, actions: list[dict[str, Any]], version: int) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item={
                    "pk": key,
                    # DynamoDB's native numeric representation rejects Python floats,
                    # and tau2 tool arguments/results legitimately contain prices.
                    # Keep the complete action log JSON-shaped instead.
                    "actions_json": json.dumps(actions),
                    "version": version + 1,
                    "expires_at": int(time.time()) + self._ttl_seconds,
                },
                ConditionExpression="attribute_not_exists(pk) OR version = :version",
                ExpressionAttributeValues={":version": version},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True
