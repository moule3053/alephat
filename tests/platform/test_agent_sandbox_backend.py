import json
from datetime import timedelta

import httpx
import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND, INVALID_PATH
from langchain_core.messages import AIMessage
from temporalio import activity
from temporalio.client import WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.deepagents import DeepAgentsPlugin
from temporalio.deepagents.testing import mock_model_provider
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from alephat_platform.common.temporal import (
    SUBMIT_TURN_UPDATE,
    TASK_WORKFLOW_NAME,
    TaskWorkflowInput,
    TurnAccepted,
    TurnEnvelope,
)
from alephat_platform.harness.agent_sandbox_backend import AgentSandboxBackend
from alephat_platform.harness.contracts import (
    COMPLETE_TURN_ACTIVITY,
    PREPARE_TURN_ACTIVITY,
    RECORD_STEP_ACTIVITY,
    CompleteTurnInput,
    PreparedTurn,
    PrepareTurnInput,
    RecordStepInput,
)
from alephat_platform.harness.sandboxes import AgentSandboxProvider, SandboxRef
from alephat_platform.harness.worker import WORKFLOW_PASSTHROUGH_MODULES
from alephat_platform.harness.workflows import TaskAgentWorkflow


@pytest.mark.asyncio
async def test_execute_uses_shell_and_preserves_stdout_and_stderr():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"exit_code": 7, "stdout": "output\n", "stderr": "failure\n"},
        )

    backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox.test:8888",
        transport=httpx.MockTransport(handler),
    )

    result = await backend.aexecute("printf 'hello'\necho done")

    assert result.exit_code == 7
    assert result.output == "output\nfailure\n"
    assert json.loads(requests[0].content) == {
        "command": (
            "/bin/sh -lc 'mkdir -p -- /app/repo && cd -- /app/repo && "
            "printf '\"'\"'hello'\"'\"'\necho done'"
        )
    }


@pytest.mark.asyncio
async def test_file_transfer_is_confined_to_runtime_workspace():
    uploaded: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execute":
            return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
        if request.url.path == "/upload":
            uploaded.append(request.content)
            return httpx.Response(200, json={"message": "uploaded"})
        if request.url.path.startswith("/download/"):
            return httpx.Response(200, content=b"saved content")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox.test:8888",
        transport=httpx.MockTransport(handler),
    )

    upload = await backend.aupload_files([("/nested/file.txt", b"saved content")])
    download = await backend.adownload_files([("/app/nested/file.txt")])

    assert upload[0].error is None
    assert b'filename="repo/nested/file.txt"' in uploaded[0]
    assert download[0].content == b"saved content"
    assert download[0].error is None


@pytest.mark.asyncio
async def test_file_transfer_normalizes_errors():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox.test:8888",
        transport=httpx.MockTransport(handler),
    )

    missing = await backend.adownload_files(["/missing.txt"])
    invalid = await backend.adownload_files(["relative.txt"])

    assert missing[0].error == FILE_NOT_FOUND
    assert invalid[0].error == INVALID_PATH


@pytest.mark.asyncio
async def test_large_edit_keeps_temporary_files_in_workspace():
    uploads: list[bytes] = []
    commands: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload":
            uploads.append(request.content)
            return httpx.Response(200, json={"message": "uploaded"})
        if request.url.path == "/execute":
            command = json.loads(request.content)["command"]
            commands.append(command)
            return httpx.Response(
                200,
                json={"exit_code": 0, "stdout": '{"count": 1}', "stderr": ""},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox.test:8888",
        transport=httpx.MockTransport(handler),
    )

    result = await backend.aedit("/large.txt", "x" * 30_000, "y" * 30_000)

    assert result.error is None
    assert result.path == "/large.txt"
    assert len(uploads) == 2
    assert all(b'filename="repo/.deepagents-tmp/edit-' in upload for upload in uploads)
    assert any("/app/repo/.deepagents-tmp" in command for command in commands)
    assert not any("/tmp/.deepagents_edit_" in command for command in commands)


@pytest.mark.asyncio
async def test_resolved_agent_sandbox_has_deep_agents_backend(monkeypatch):
    provider = AgentSandboxProvider()

    async def kube_request(_method: str, path: str, **_kwargs: object) -> dict[str, object]:
        assert path.endswith("/sandboxes/sandbox-1")
        return {
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "serviceFQDN": "sandbox-1.alephat-sandboxes.svc.cluster.local",
                "podIPs": ["10.32.2.26"],
            }
        }

    monkeypatch.setattr(provider, "_request", kube_request)

    ref = await provider._resolve_ref(SandboxRef(provider="agent_sandbox", sandbox_id="sandbox-1"))

    assert isinstance(ref.handle, AgentSandboxBackend)
    assert ref.handle.id == "sandbox-1"
    assert ref.handle.workspace == "/app/repo"
    assert ref.handle.fallback_url == "http://10.32.2.26:8888"

    seen_hosts: list[str] = []

    async def dns_race_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        if request.url.host == "sandbox-1.alephat-sandboxes.svc.cluster.local":
            raise httpx.ConnectError("headless service DNS not propagated yet", request=request)
        return httpx.Response(200, json={"exit_code": 0, "stdout": "cloned\n", "stderr": ""})

    fallback_backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox-1.alephat-sandboxes.svc.cluster.local:8888",
        fallback_url="http://10.32.2.26:8888",
        transport=httpx.MockTransport(dns_race_handler),
    )
    res = await fallback_backend.aexecute("git status")
    assert res.exit_code == 0
    assert seen_hosts == ["sandbox-1.alephat-sandboxes.svc.cluster.local", "10.32.2.26"]


@pytest.mark.asyncio
async def test_agent_sandbox_executes_through_temporal_deep_agents_workflow():
    executed: list[str] = []
    events: list[str] = []
    completed: list[CompleteTurnInput] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        executed.append(payload["command"])
        return httpx.Response(200, json={"exit_code": 0, "stdout": "ok\n", "stderr": ""})

    backend = AgentSandboxBackend(
        "sandbox-1",
        "http://sandbox.test:8888",
        transport=httpx.MockTransport(handler),
    )

    @activity.defn(name=PREPARE_TURN_ACTIVITY)
    async def fake_prepare(req: PrepareTurnInput) -> PreparedTurn:
        return PreparedTurn(
            task_id=req.task_id,
            run_id=req.run_id,
            agent_type="coding",
            repo="acme/repo",
            base_ref="main",
            model="openai:gpt-4o-mini",
            effort=None,
            sandbox_provider="agent_sandbox",
            sandbox_id="sandbox-1",
            sandbox_is_stub=False,
            workspace_path="/app/repo",
        )

    @activity.defn(name=RECORD_STEP_ACTIVITY)
    async def fake_record(req: RecordStepInput) -> None:
        events.append(req.event_type)

    @activity.defn(name=COMPLETE_TURN_ACTIVITY)
    async def fake_complete(req: CompleteTurnInput) -> None:
        completed.append(req)

    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "printf deep-agent-ok"},
                            "id": "execute-1",
                        }
                    ],
                ),
                AIMessage(content="deep agent complete"),
            ]
        ),
        backends={"agent_sandbox": lambda _sid: backend},
        passthrough_modules=WORKFLOW_PASSTHROUGH_MODULES,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        from temporalio.client import Client

        cfg = env.client.config()
        cfg["plugins"] = [plugin]
        client = Client(**cfg)

        async with Worker(
            client,
            task_queue="test-agent-sandbox",
            workflows=[TaskAgentWorkflow],
            activities=[fake_prepare, fake_record, fake_complete],
        ):
            start_op = WithStartWorkflowOperation(
                TASK_WORKFLOW_NAME,
                TaskWorkflowInput(task_id="task-1", org_id="org-1"),
                id="alephat-task:task-1",
                task_queue="test-agent-sandbox",
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            accepted = await client.execute_update_with_start_workflow(
                SUBMIT_TURN_UPDATE,
                TurnEnvelope(
                    turn_id="turn-1",
                    message_id="msg-1",
                    content="run the command",
                ),
                start_workflow_operation=start_op,
                id="turn-1",
                result_type=TurnAccepted,
            )
            assert accepted.mode == "queued"

            for _ in range(50):
                if completed:
                    break
                await env.sleep(timedelta(milliseconds=100))

            handle = client.get_workflow_handle("alephat-task:task-1")
            await handle.terminate(reason="test_done")

    assert len(completed) == 1
    assert completed[0].success is True
    assert completed[0].final_message == "deep agent complete"
    assert executed == [
        "/bin/sh -lc 'mkdir -p -- /app/repo && cd -- /app/repo && printf deep-agent-ok'"
    ]
    assert "tool_started" in events
