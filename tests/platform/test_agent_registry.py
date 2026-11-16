"""Tests for Temporal DeepAgents workflow durability, HITL approvals, follow-ups, and model routing."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.deepagents import DeepAgentsPlugin
from temporalio.deepagents.testing import mock_model_provider
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from alephat_platform.common.config import clear_settings_cache
from alephat_platform.common.temporal import (
    DECIDE_APPROVAL_UPDATE,
    GET_STATE_QUERY,
    SUBMIT_TURN_UPDATE,
    TASK_WORKFLOW_NAME,
    ApprovalResolution,
    PlatformPayloadCodec,
    TaskWorkflowInput,
    TurnAccepted,
    TurnEnvelope,
    WorkflowStateSnapshot,
)
from alephat_platform.harness.agents import MODEL_PROVIDER, PROMPT_BUILDER, PlatformModelSpec
from alephat_platform.harness.contracts import (
    COMPLETE_TURN_ACTIVITY,
    ENTER_PARKED_ACTIVITY,
    EXIT_PARKED_ACTIVITY,
    PREPARE_TURN_ACTIVITY,
    RECORD_STEP_ACTIVITY,
    CompleteTurnInput,
    EnterParkedInput,
    ExitParkedInput,
    PreparedTurn,
    PrepareTurnInput,
    RecordStepInput,
)
from alephat_platform.harness.sandboxes import SANDBOX_REGISTRY
from alephat_platform.harness.worker import WORKFLOW_PASSTHROUGH_MODULES
from alephat_platform.harness.workflows import TaskAgentWorkflow


def test_supported_agent_types():
    for name in ("coding", "reviewer", "analyzer", "chat"):
        assert PROMPT_BUILDER.validate_agent_type(name) == name
    with pytest.raises(KeyError):
        PROMPT_BUILDER.validate_agent_type("unknown")


def test_runtime_instructions_include_repo_rules_and_plan_mode():
    instructions = PROMPT_BUILDER.runtime_instructions(
        {
            "custom_instructions": "Always run the contract tests.",
            "plan_mode": True,
        }
    )
    assert "Always run the contract tests." in instructions
    assert "request plan approval before making changes" in instructions


def test_model_effort_kwargs_and_spec_roundtrip():
    spec = PlatformModelSpec(model="google:gemini-3.5-flash", effort="high")
    decoded = PlatformModelSpec.decode(spec.encode())
    assert decoded == spec
    assert MODEL_PROVIDER.effort_kwargs("google_genai", "xhigh") == {"thinking_level": "high"}
    assert MODEL_PROVIDER.effort_kwargs("openai", "medium") == {"reasoning_effort": "medium"}


def test_litellm_disables_sdk_retries_and_uses_openai_effort(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "litellm")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000")
    clear_settings_cache()
    try:
        with patch("langchain_openai.ChatOpenAI") as chat_openai:
            MODEL_PROVIDER("google:gemini-3.5-flash#medium")
    finally:
        clear_settings_cache()

    kwargs = chat_openai.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["reasoning_effort"] == "medium"
    assert "thinking_level" not in kwargs


@pytest.mark.asyncio
async def test_payload_codec_compresses_and_encrypts_history():
    key = Fernet.generate_key().decode()
    codec = PlatformPayloadCodec(key)
    raw_bytes = b"durable-deepagents-payload-" * 120
    original = Payload(metadata={"encoding": b"json/plain"}, data=raw_bytes)

    encoded = await codec.encode([original])
    assert encoded[0].metadata["encoding"] == b"binary/encrypted-fernet"
    assert bytes(encoded[0].data) != raw_bytes

    decoded = await codec.decode(encoded)
    assert bytes(decoded[0].data) == raw_bytes
    assert decoded[0].metadata["encoding"] == b"json/plain"


def test_temporal_tls_builder_loads_mtls_certificates(tmp_path):
    from temporalio.service import TLSConfig

    from alephat_platform.common.config import Settings
    from alephat_platform.common.temporal import TemporalTlsBuilder

    assert TemporalTlsBuilder(Settings(temporal_tls_enabled=False)).build() is False

    ca = tmp_path / "ca.crt"
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    ca.write_bytes(b"CA-PEM")
    cert.write_bytes(b"CERT-PEM")
    key.write_bytes(b"KEY-PEM")

    tls_cfg = TemporalTlsBuilder(
        Settings(
            temporal_tls_enabled=True,
            temporal_tls_ca_cert_path=str(ca),
            temporal_tls_client_cert_path=str(cert),
            temporal_tls_client_key_path=str(key),
            temporal_tls_server_name="temporal.alephat.svc.cluster.local",
        )
    ).build()
    assert isinstance(tls_cfg, TLSConfig)
    assert tls_cfg.server_root_ca_cert == b"CA-PEM"
    assert tls_cfg.client_cert == b"CERT-PEM"
    assert tls_cfg.client_private_key == b"KEY-PEM"
    assert tls_cfg.domain == "temporal.alephat.svc.cluster.local"


@pytest.mark.asyncio
async def test_workflow_plan_approval_hitl_and_followup_turns():
    parked_events: list[EnterParkedInput] = []
    resumed_events: list[ExitParkedInput] = []
    completions: list[CompleteTurnInput] = []

    @activity.defn(name=PREPARE_TURN_ACTIVITY)
    async def fake_prepare(req: PrepareTurnInput) -> PreparedTurn:
        return PreparedTurn(
            task_id=req.task_id,
            run_id=req.run_id,
            agent_type="coding",
            repo=None,
            base_ref="main",
            model="openai:gpt-4o-mini",
            effort=None,
            sandbox_provider="opensandbox",
            sandbox_id=req.sandbox_id or "stub-box-1",
            sandbox_is_stub=True,
            workspace_path=None,
        )

    @activity.defn(name=RECORD_STEP_ACTIVITY)
    async def fake_record(_req: RecordStepInput) -> None:
        return None

    @activity.defn(name=ENTER_PARKED_ACTIVITY)
    async def fake_park(req: EnterParkedInput) -> str:
        parked_events.append(req)
        return "approval-123"

    @activity.defn(name=EXIT_PARKED_ACTIVITY)
    async def fake_unpark(req: ExitParkedInput) -> None:
        resumed_events.append(req)

    @activity.defn(name=COMPLETE_TURN_ACTIVITY)
    async def fake_complete(req: CompleteTurnInput) -> None:
        completions.append(req)

    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "request_plan_approval",
                            "args": {"plan": "1. Refactor worker\n2. Run tests"},
                            "id": "plan-call-1",
                        }
                    ],
                ),
                AIMessage(content="First turn finished after approval."),
                AIMessage(content="Follow-up turn finished with prior context."),
            ]
        ),
        backends=SANDBOX_REGISTRY.backend_factories(),
        passthrough_modules=WORKFLOW_PASSTHROUGH_MODULES,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        cfg = env.client.config()
        cfg["plugins"] = [plugin]
        client = Client(**cfg)

        async with Worker(
            client,
            task_queue="test-hitl-queue",
            workflows=[TaskAgentWorkflow],
            activities=[fake_prepare, fake_record, fake_park, fake_unpark, fake_complete],
        ):
            wf_id = "alephat-task:hitl-1"
            start_op = WithStartWorkflowOperation(
                TASK_WORKFLOW_NAME,
                TaskWorkflowInput(task_id="hitl-1", org_id="org-1"),
                id=wf_id,
                task_queue="test-hitl-queue",
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            await client.execute_update_with_start_workflow(
                SUBMIT_TURN_UPDATE,
                TurnEnvelope(turn_id="t1", message_id="m1", content="Implement feature"),
                start_workflow_operation=start_op,
                id="t1",
                result_type=TurnAccepted,
            )

            handle = client.get_workflow_handle(wf_id)
            for _ in range(50):
                if parked_events:
                    break
                await env.sleep(timedelta(milliseconds=50))

            state = await handle.query(GET_STATE_QUERY, result_type=WorkflowStateSnapshot)
            assert state.pending_approval is not None
            assert state.pending_approval.kind == "plan_approval"
            assert "Refactor worker" in state.pending_approval.prompt

            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    DECIDE_APPROVAL_UPDATE,
                    ApprovalResolution(decision="invalid"),
                    result_type=bool,
                )

            approved = await handle.execute_update(
                DECIDE_APPROVAL_UPDATE,
                ApprovalResolution(approval_id="approval-123", decision="approved"),
                result_type=bool,
            )
            assert approved is True

            for _ in range(50):
                if len(completions) >= 1:
                    break
                await env.sleep(timedelta(milliseconds=50))

            assert completions[0].success is True
            assert completions[0].final_message == "First turn finished after approval."
            assert len(resumed_events) == 1

            followup_op = WithStartWorkflowOperation(
                TASK_WORKFLOW_NAME,
                TaskWorkflowInput(task_id="hitl-1", org_id="org-1"),
                id=wf_id,
                task_queue="test-hitl-queue",
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            await client.execute_update_with_start_workflow(
                SUBMIT_TURN_UPDATE,
                TurnEnvelope(turn_id="t2", message_id="m2", content="Now add docs"),
                start_workflow_operation=followup_op,
                id="t2",
                result_type=TurnAccepted,
            )

            for _ in range(50):
                if len(completions) >= 2:
                    break
                await env.sleep(timedelta(milliseconds=50))

            assert completions[1].success is True
            assert completions[1].final_message == "Follow-up turn finished with prior context."
            await handle.terminate(reason="done")
