"""Lambda Function URL handler for the tau2 retail tools.

Every endpoint call gets an isolated logical store keyed by both the evaluation
run and Oumi conversation id.  The actual retail database is reconstructed from
the canonical bundled database and the session's successful writes; this avoids
putting tau2's multi-megabyte database into a DynamoDB item.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from functools import lru_cache
from typing import Any, Mapping

from tau2.domains.retail.data_model import RetailDB
from tau2.domains.retail.environment import get_environment
from tau2.domains.retail.utils import RETAIL_DB_PATH

from lambda_runtime.store import DynamoStore, Session, SessionStore


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_SESSION_ID_LENGTH = 256
_MAX_WRITE_RETRIES = 5
_MAX_ERROR_LENGTH = 180


@lru_cache(maxsize=1)
def _base_db() -> RetailDB:
    """Load the immutable retail database once per warm Lambda environment."""
    return RetailDB.load(RETAIL_DB_PATH)


@lru_cache(maxsize=1)
def _store() -> DynamoStore:
    return DynamoStore(os.environ["SESSIONS_TABLE"])


def _response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(_json_value(body)),
    }


def _json_value(value: Any) -> Any:
    """Render tau2 Pydantic responses into Function URL JSON."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), "")


def _authorized(headers: Mapping[str, str]) -> bool:
    expected = os.environ.get("TOOL_ENDPOINT_TOKEN", "")
    return bool(expected) and hmac.compare_digest(
        _header(headers, "authorization"), f"Bearer {expected}"
    )


def _request(event: dict[str, Any]) -> tuple[str, str, dict[str, Any], str, str]:
    """Extract and validate a Function URL request.

    The supported routes deliberately mirror the local FastAPI service:
    ``/tools/<tool>`` for manual calls and ``/runs/<run>/tools/<tool>`` for
    evaluation runs.  The run prefix is required for isolation when a dataset
    is reused across evaluations.
    """
    headers = event.get("headers") or {}
    try:
        arguments = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("body is not JSON") from error
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")

    parts = [part for part in str(event.get("rawPath") or "").split("/") if part]
    if len(parts) == 2 and parts[0] == "tools":
        run_id, tool_name = "default", parts[1]
    elif len(parts) == 4 and parts[0] == "runs" and parts[2] == "tools":
        run_id, tool_name = parts[1], parts[3]
    else:
        raise ValueError("route must be /tools/<tool> or /runs/<run>/tools/<tool>")
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run id must be 1-64 letters, digits, '.', '_' or '-'")

    session_id = _header(headers, "x-oumi-session-id")
    call_id = _header(headers, "x-oumi-call-id")
    if not session_id or len(session_id) > _MAX_SESSION_ID_LENGTH:
        raise ValueError("X-Oumi-Session-Id is required and must be at most 256 characters")
    if not call_id:
        raise ValueError("X-Oumi-Call-Id is required")
    return run_id, tool_name, arguments, session_id, call_id


def _session_key(run_id: str, session_id: str) -> str:
    return f"session#{run_id}#{session_id}"


def _environment(session: Session):
    """Build the session's retail environment by replaying its prior writes."""
    environment = get_environment(db=_base_db().model_copy(deep=True))
    for action in session.actions:
        environment.use_tool(action["tool"], **action["arguments"])
    return environment


def _prior_action(session: Session, call_id: str) -> dict[str, Any] | None:
    for action in session.actions:
        if action["call_id"] == call_id:
            return action
    return None


def handle_call(
    *,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str,
    call_id: str,
    store: SessionStore,
) -> tuple[int, Any]:
    """Run one tool call with per-run/session isolation and retry safety."""
    key = _session_key(run_id, session_id)
    for _ in range(_MAX_WRITE_RETRIES):
        session = store.read(key)
        previous = _prior_action(session, call_id)
        if previous is not None:
            return 200, previous["output"]

        environment = _environment(session)
        if not environment.tools or not environment.tools.has_tool(tool_name):
            return 400, {"error": f"unknown tool {tool_name!r}"}
        mutates = environment.tools.tool_mutates_state(tool_name)
        try:
            output = _json_value(environment.use_tool(tool_name, **arguments))
        except Exception as error:
            return 400, {"error": str(error)[:_MAX_ERROR_LENGTH]}
        if not mutates:
            return 200, output

        actions = [
            *session.actions,
            {
                "call_id": call_id,
                "tool": tool_name,
                "arguments": arguments,
                "output": output,
            },
        ]
        if store.write(key, actions, session.version):
            return 200, output
    return 503, {"error": "tool state was updated concurrently; retry this call"}


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Serve one authenticated Oumi endpoint-tool request."""
    del context
    headers = event.get("headers") or {}
    if not _authorized(headers):
        return _response(401, {"error": "unauthorized"})
    try:
        run_id, tool_name, arguments, session_id, call_id = _request(event)
    except ValueError as error:
        return _response(400, {"error": str(error)})
    status, output = handle_call(
        run_id=run_id,
        tool_name=tool_name,
        arguments=arguments,
        session_id=session_id,
        call_id=call_id,
        store=_store(),
    )
    return _response(status, output)
