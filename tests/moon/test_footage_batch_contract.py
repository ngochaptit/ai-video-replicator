from moon.drive_bridge import MoonDriveBridge
from moon.handoff import AgentHandoffService


def test_footage_batch_contract_requires_active_batch_identity() -> None:
    contract = AgentHandoffService._output_contract("footage")

    assert contract["required"] == ["batch_id", "clips"]
    assert any(
        "request.task.batch_id" in rule for rule in contract.get("rules") or []
    )

    schema = MoonDriveBridge._response_schema(
        contract,
        stage="footage",
        footage_batch=True,
    )
    approved_payload_schema = schema["allOf"][1]["then"]["properties"]["payload"]
    assert "batch_id" in approved_payload_schema["required"]
