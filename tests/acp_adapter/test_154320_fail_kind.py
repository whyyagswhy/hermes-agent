"""#154320 — admitted_turn_failed must include fail_kind when failure_reason is set.

RED on main: GatewayClientError string is "admitted_turn_failed" (not JSON), so
json.loads raises and the ACP framework wraps it as {"details":"admitted_turn_failed"}
with no fail_kind field. The test below asserts fail_kind IS present.
"""
import json
import pytest
from acp.schema import TextContentBlock
from acp_adapter.gateway_server import GatewayACPAgent
from hermes_cli.gateway_client import GatewayClientError
from unittest.mock import AsyncMock


def agent_with_admission():
    agent = GatewayACPAgent()
    agent._snapshots["s"] = {}
    agent._gateway = AsyncMock()
    agent._gateway.rpc.return_value = {"admission_id": "current"}
    agent._conn = AsyncMock()
    return agent


async def complete_with_reason(agent, admission_id, outcome, text, failure_reason=None):
    payload = {"outcome": outcome, "text": text}
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    await agent._project(
        {
            "session_id": "s",
            "type": "message.complete",
            "admission_id": admission_id,
            "payload": payload,
        }
    )


@pytest.mark.asyncio
async def test_failed_turn_without_reason_still_raises_admitted_turn_failed():
    """Backward-compat: no failure_reason -> error string still matches admitted_turn_failed."""
    agent = agent_with_admission()
    await complete_with_reason(agent, "current", "failed", "Provider failed.")
    with pytest.raises(GatewayClientError, match="admitted_turn_failed"):
        await agent.prompt([TextContentBlock(type="text", text="hi")], "s")


@pytest.mark.asyncio
async def test_failed_turn_with_reason_includes_fail_kind_in_serialised_data():
    """fail_kind must appear in the JSON-serialised exception so the ACP framework
    forwards it in error.data to the client (#154320)."""
    agent = agent_with_admission()
    await complete_with_reason(
        agent, "current", "failed", "Auth failure.", failure_reason="provider_auth_failed"
    )
    with pytest.raises(GatewayClientError) as exc_info:
        await agent.prompt([TextContentBlock(type="text", text="hi")], "s")
    # Must be valid JSON — the ACP connection layer uses json.loads(str(exc)) as error data.
    data = json.loads(str(exc_info.value))
    assert data["details"] == "admitted_turn_failed"
    assert data["fail_kind"] == "provider_auth_failed"


@pytest.mark.asyncio
async def test_failed_turn_rate_limited_includes_fail_kind():
    """rate_limited is a distinct failure_reason; must also round-trip."""
    agent = agent_with_admission()
    await complete_with_reason(
        agent, "current", "failed", "Rate limit.", failure_reason="rate_limited"
    )
    with pytest.raises(GatewayClientError) as exc_info:
        await agent.prompt([TextContentBlock(type="text", text="hi")], "s")
    data = json.loads(str(exc_info.value))
    assert data["fail_kind"] == "rate_limited"
