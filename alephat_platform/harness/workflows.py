"""Temporal Workflows for durable DeepAgents coding tasks and scheduled automations."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

from deepagents.backends.local_shell import LocalShellBackend
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.load import dumpd, load
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages import message_to_dict as langchain_message_to_dict
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.deepagents import TemporalBackend, create_temporal_deep_agent
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.workflow import ActivityConfig

from alephat_platform.common.temporal import (
    CANCEL_CURRENT_UPDATE,
    DECIDE_APPROVAL_UPDATE,
    GET_STATE_QUERY,
    SCHEDULE_WORKFLOW_NAME,
    SUBMIT_TURN_UPDATE,
    TASK_WORKFLOW_NAME,
    ApprovalResolution,
    PendingApprovalView,
    ScheduledAutomationInput,
    TaskWorkflowInput,
    TurnAccepted,
    TurnEnvelope,
    WorkflowStateSnapshot,
)
from alephat_platform.harness.agent_sandbox_backend import AgentSandboxBackend
from alephat_platform.harness.agents import (
    PLAN_APPROVAL_TOOL,
    PROMPT_BUILDER,
    USER_GUIDANCE_TOOL,
    PlatformModelSpec,
)
from alephat_platform.harness.contracts import (
    COMPLETE_TURN_ACTIVITY,
    ENTER_PARKED_ACTIVITY,
    EXIT_PARKED_ACTIVITY,
    INVOKE_MCP_TOOL_ACTIVITY,
    PREPARE_TURN_ACTIVITY,
    RECORD_STEP_ACTIVITY,
    RENDER_SLACK_CARD_ACTIVITY,
    TRIGGER_SCHEDULED_AUTOMATION_ACTIVITY,
    UPLOAD_SLACK_FILE_ACTIVITY,
    CompleteTurnInput,
    EnterParkedInput,
    ExitParkedInput,
    InvokeMcpToolInput,
    McpToolWireSpec,
    PreparedTurn,
    PrepareTurnInput,
    RecordStepInput,
    RenderSlackCardInput,
    UploadSlackFileInput,
)
from alephat_platform.harness.sandboxes import StubSandboxBackend

DEFAULT_IDLE_TIMEOUT = timedelta(hours=1)
MAX_SEEN_TURNS = 512


class WorkflowDynamicToolFactory:
    """Construct replay-safe workflow tools that execute registered Temporal Activities."""

    def __init__(self, *, task_id: str, run_id: str) -> None:
        self._task_id = task_id
        self._run_id = run_id

    def mcp_tool(self, spec: McpToolWireSpec) -> BaseTool:
        async def invoke(**kwargs: object) -> str:
            return await workflow.execute_activity(
                INVOKE_MCP_TOOL_ACTIVITY,
                InvokeMcpToolInput(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    namespaced_name=spec.namespaced_name,
                    server_id=spec.server_id,
                    tool_name=spec.tool_name,
                    server_config=spec.server_config,
                    args=dict(kwargs),
                ),
                result_type=str,
                start_to_close_timeout=timedelta(seconds=150),
                retry_policy=RetryPolicy(maximum_attempts=2),
                summary=f"mcp:{spec.namespaced_name}",
            )

        return StructuredTool(
            name=spec.namespaced_name,
            description=spec.description,
            args_schema=spec.args_schema,
            coroutine=invoke,
        )

    def slack_tools(self, *, sandbox_provider: str, sandbox_id: str) -> list[BaseTool]:
        async def render_slack_card(spec_json: str, fallback_text: str) -> dict[str, object]:
            """Render a rich @json-render/chat Slack card in the task thread."""
            return await workflow.execute_activity(
                RENDER_SLACK_CARD_ACTIVITY,
                RenderSlackCardInput(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    spec_json=spec_json,
                    fallback_text=fallback_text,
                ),
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
                summary="slack:render_card",
            )

        async def upload_slack_file(path: str, title: str) -> dict[str, object]:
            """Upload a file from the sandbox workspace to the Slack thread."""
            return await workflow.execute_activity(
                UPLOAD_SLACK_FILE_ACTIVITY,
                UploadSlackFileInput(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    sandbox_provider=sandbox_provider,
                    sandbox_id=sandbox_id,
                    path=path,
                    title=title,
                ),
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
                summary="slack:upload_file",
            )

        return [
            StructuredTool.from_function(
                coroutine=render_slack_card,
                name="render_slack_card",
                description="Render and post a rich @json-render/chat Slack card in the thread.",
            ),
            StructuredTool.from_function(
                coroutine=upload_slack_file,
                name="upload_slack_file",
                description="Upload a binary or text artifact from the sandbox to the Slack thread.",
            ),
        ]


class WorkflowTurnMiddleware(AgentMiddleware[AgentState[object], None]):
    """In-workflow middleware handling live steering, cancellation, and step reporting."""

    name = "WorkflowTurnMiddleware"

    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        model: str,
        slack_enabled: bool,
        extra_tools: list[BaseTool],
        drain_steering: Callable[[], list[TurnEnvelope]],
        is_cancelled: Callable[[], bool],
    ) -> None:
        self._task_id = task_id
        self._run_id = run_id
        self._model = model
        self._slack_enabled = slack_enabled
        self.tools = tuple(extra_tools)
        self._drain_steering = drain_steering
        self._is_cancelled = is_cancelled
        self.latest_messages: list[BaseMessage] = []

    def dump_transcript(self, messages: list[BaseMessage]) -> list[dict[str, object]]:
        dumped: list[dict[str, object]] = []
        for msg in messages:
            raw = dumpd(msg)
            if isinstance(raw, dict):
                dumped.append(raw)
            else:
                dumped.append(langchain_message_to_dict(msg))
        return dumped

    async def abefore_model(
        self, state: AgentState[object], runtime: object
    ) -> dict[str, object] | None:
        del runtime
        if self._is_cancelled():
            raise asyncio.CancelledError("Task cancelled")
        raw_msgs = state.get("messages")
        if isinstance(raw_msgs, list):
            self.latest_messages = [m for m in raw_msgs if isinstance(m, BaseMessage)]
        steered = self._drain_steering()
        if not steered:
            return None
        injected = [
            HumanMessage(id=item.message_id, content=item.content)
            for item in steered
            if item.content
        ]
        if not injected:
            return None
        return {"messages": injected}

    async def awrap_model_call(
        self,
        request: ModelRequest[None],
        handler: Callable[[ModelRequest[None]], Awaitable[ModelResponse[object]]],
    ) -> ModelResponse[object]:
        await workflow.execute_activity(
            RECORD_STEP_ACTIVITY,
            RecordStepInput(
                task_id=self._task_id,
                run_id=self._run_id,
                event_type="model_started",
                payload={"model": self._model},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            summary="step:model_started",
        )
        response = await handler(request)
        produced = list(response.result) if hasattr(response, "result") else []
        combined = [
            *(m for m in request.messages if isinstance(m, BaseMessage)),
            *(m for m in produced if isinstance(m, BaseMessage)),
        ]
        self.latest_messages = combined
        preview_text: str | None = None
        if self._slack_enabled:
            for msg in reversed(produced):
                if isinstance(msg, AIMessage) and not msg.tool_calls:
                    txt = PROMPT_BUILDER.extract_text(msg).strip()
                    if txt:
                        preview_text = txt
                        break
        await workflow.execute_activity(
            RECORD_STEP_ACTIVITY,
            RecordStepInput(
                task_id=self._task_id,
                run_id=self._run_id,
                event_type="model_finished",
                payload={"model": self._model},
                messages=self.dump_transcript(combined),
                preview_text=preview_text,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            summary="step:model_finished",
        )
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[object]]],
    ) -> ToolMessage | Command[object]:
        name = str(request.tool_call.get("name") or "tool")
        raw_msgs = request.state.get("messages")
        if isinstance(raw_msgs, list):
            self.latest_messages = [m for m in raw_msgs if isinstance(m, BaseMessage)]
        if name in {"request_plan_approval", "request_user_guidance"} or name.startswith("mcp_"):
            return await handler(request)

        payload: dict[str, object] = {"tool": name}
        await workflow.execute_activity(
            RECORD_STEP_ACTIVITY,
            RecordStepInput(
                task_id=self._task_id,
                run_id=self._run_id,
                event_type="tool_started",
                payload=payload,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            summary=f"step:tool_started:{name}",
        )
        try:
            result = await handler(request)
        except Exception as exc:
            await workflow.execute_activity(
                RECORD_STEP_ACTIVITY,
                RecordStepInput(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    event_type="tool_finished",
                    payload={**payload, "ok": False, "error": str(exc)},
                ),
                start_to_close_timeout=timedelta(seconds=30),
                summary=f"step:tool_failed:{name}",
            )
            raise
        await workflow.execute_activity(
            RECORD_STEP_ACTIVITY,
            RecordStepInput(
                task_id=self._task_id,
                run_id=self._run_id,
                event_type="tool_finished",
                payload={**payload, "ok": True},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            summary=f"step:tool_finished:{name}",
        )
        return result


@workflow.defn(name=TASK_WORKFLOW_NAME)
class TaskAgentWorkflow:
    """Long-running durable coding-agent entity workflow powered by DeepAgentsPlugin."""

    @workflow.init
    def __init__(self, params: TaskWorkflowInput) -> None:
        self._task_id = params.task_id
        self._org_id = params.org_id
        self._sandbox_id = params.sandbox_id
        self._transcript: list[dict[str, object]] = list(params.transcript)
        self._seen_turn_ids: list[str] = list(params.seen_turn_ids)
        self._pending_turns: deque[TurnEnvelope] = deque(params.pending_turns)
        self._steering: deque[TurnEnvelope] = deque()
        self._pending_approval: PendingApprovalView | None = None
        self._approval_resolution: ApprovalResolution | None = None
        self._cancel_requested = False
        self._cancel_reason: str | None = None
        self._active_run_id: str | None = None
        self._turn_in_progress = False
        self._current_turn_task: asyncio.Task[None] | None = None
        self.stream = WorkflowStream(prior_state=params.stream_state)

    def _remember_turn_id(self, turn_id: str) -> bool:
        if turn_id in self._seen_turn_ids:
            return False
        self._seen_turn_ids.append(turn_id)
        if len(self._seen_turn_ids) > MAX_SEEN_TURNS:
            self._seen_turn_ids = self._seen_turn_ids[-MAX_SEEN_TURNS:]
        return True

    def _drain_steering(self) -> list[TurnEnvelope]:
        items = list(self._steering)
        self._steering.clear()
        return items

    @workflow.query(name=GET_STATE_QUERY)
    def get_state(self) -> WorkflowStateSnapshot:
        return WorkflowStateSnapshot(
            task_id=self._task_id,
            active_run_id=self._active_run_id,
            turn_in_progress=self._turn_in_progress,
            pending_turns=len(self._pending_turns),
            pending_approval=self._pending_approval,
            sandbox_id=self._sandbox_id,
            transcript_count=len(self._transcript),
        )

    @workflow.update(name=SUBMIT_TURN_UPDATE)
    async def submit_turn(self, turn: TurnEnvelope) -> TurnAccepted:
        await asyncio.sleep(0)
        if not self._remember_turn_id(turn.turn_id):
            return TurnAccepted(
                run_id=self._active_run_id or turn.run_id or self._task_id,
                mode="duplicate",
            )

        if self._pending_approval is not None and self._pending_approval.kind == "agent_question":
            self._approval_resolution = ApprovalResolution(
                decision="respond",
                comment=turn.content,
            )
            return TurnAccepted(
                run_id=self._active_run_id or self._task_id,
                mode="resumed",
            )

        if self._turn_in_progress and self._pending_approval is None:
            self._steering.append(turn)
            return TurnAccepted(
                run_id=self._active_run_id or self._task_id,
                mode="steered",
            )

        self._cancel_requested = False
        self._cancel_reason = None
        assigned_run_id = turn.run_id or str(workflow.uuid4())
        self._pending_turns.append(replace(turn, run_id=assigned_run_id))
        return TurnAccepted(run_id=assigned_run_id, mode="queued")

    @workflow.update(name=DECIDE_APPROVAL_UPDATE)
    async def decide_approval(self, resolution: ApprovalResolution) -> bool:
        await asyncio.sleep(0)
        self._approval_resolution = resolution
        return True

    @decide_approval.validator
    def validate_decide_approval(self, resolution: ApprovalResolution) -> None:
        if resolution.decision not in {"approved", "rejected", "respond"}:
            raise ValueError("decision must be approved, rejected, or respond")
        if self._pending_approval is None:
            raise ValueError("Task has no pending approval or question")

    @workflow.update(name=CANCEL_CURRENT_UPDATE)
    async def cancel_current(self, reason: str) -> bool:
        await asyncio.sleep(0)
        self._cancel_requested = True
        self._cancel_reason = reason
        self._pending_turns.clear()
        self._steering.clear()
        if self._current_turn_task is not None and not self._current_turn_task.done():
            self._current_turn_task.cancel()
        return True

    def _select_backend(self, prepared: PreparedTurn) -> TemporalBackend:
        opts = ActivityConfig(
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if prepared.sandbox_is_stub:
            return TemporalBackend(
                "stub",
                factory_argument=prepared.sandbox_id,
                backend_type=StubSandboxBackend,
                metadata={"id": prepared.sandbox_id},
                activity_options=opts,
            )
        if prepared.sandbox_provider == "local":
            return TemporalBackend(
                "local",
                factory_argument=prepared.sandbox_id,
                backend_type=LocalShellBackend,
                metadata={"id": prepared.sandbox_id},
                activity_options=opts,
            )
        if prepared.sandbox_provider == "daytona":
            return TemporalBackend(
                "daytona",
                factory_argument=prepared.sandbox_id,
                backend_type=AgentSandboxBackend,
                metadata={"id": prepared.sandbox_id},
                activity_options=opts,
            )
        return TemporalBackend(
            "agent_sandbox",
            factory_argument=prepared.sandbox_id,
            backend_type=AgentSandboxBackend,
            metadata={"id": prepared.sandbox_id, "workspace": "/app/repo"},
            activity_options=opts,
        )

    def _load_transcript_messages(self, dumped: list[dict[str, object]]) -> list[BaseMessage]:
        loaded: list[BaseMessage] = []
        for entry in dumped:
            try:
                obj = load(entry, allowed_objects="messages")
                if isinstance(obj, BaseMessage):
                    loaded.append(obj)
            except Exception:
                continue
        return loaded

    def _dump_messages(self, messages: list[BaseMessage]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for msg in messages:
            raw = dumpd(msg)
            if isinstance(raw, dict):
                out.append(raw)
            else:
                out.append(langchain_message_to_dict(msg))
        return out

    def _extract_interrupt_details(self, interrupts: object) -> tuple[str, str, dict[str, object]]:
        items = list(interrupts) if isinstance(interrupts, list | tuple) else [interrupts]
        first = items[0] if items else None
        val = getattr(first, "value", first)
        action_requests = val.get("action_requests") if isinstance(val, dict) else None
        if isinstance(action_requests, list) and action_requests:
            req = action_requests[0]
            if isinstance(req, dict):
                name = str(req.get("name") or "")
                args = req.get("args") if isinstance(req.get("args"), dict) else {}
                if name == "request_user_guidance":
                    question = str(args.get("question") or "Additional user guidance required.")
                    return "agent_question", question, {"question": question}
                plan = str(args.get("plan") or "Plan approval requested.")
                return "plan_approval", plan, {"plan": plan}
        fallback = str(val or "Approval required.")
        return "plan_approval", fallback, {"plan": fallback}

    async def _run_single_turn(self, turn: TurnEnvelope) -> None:
        run_id = turn.run_id or str(workflow.uuid4())
        self._active_run_id = run_id
        self._turn_in_progress = True

        try:
            prepared = await workflow.execute_activity(
                PREPARE_TURN_ACTIVITY,
                PrepareTurnInput(
                    task_id=self._task_id,
                    run_id=run_id,
                    temporal_run_id=workflow.info().run_id,
                    sandbox_id=self._sandbox_id,
                    load_history=not bool(self._transcript),
                    message_ts=turn.message_ts,
                ),
                result_type=PreparedTurn,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
                summary="alephat:prepare_turn",
            )
        except (ActivityError, ApplicationError) as exc:
            await workflow.execute_activity(
                COMPLETE_TURN_ACTIVITY,
                CompleteTurnInput(
                    task_id=self._task_id,
                    run_id=run_id,
                    success=False,
                    cancelled=self._cancel_requested,
                    final_message=str(exc),
                    error_code="turn_preparation_failed",
                ),
                start_to_close_timeout=timedelta(seconds=60),
                summary="alephat:complete_failed_prep",
            )
            return

        self._sandbox_id = prepared.sandbox_id
        if not self._transcript and prepared.prior_messages:
            prior = [
                entry
                for entry in prepared.prior_messages
                if str(
                    (entry.get("kwargs") or {}).get("id")
                    if isinstance(entry.get("kwargs"), dict)
                    else ""
                )
                != turn.message_id
            ]
            self._transcript = prior

        history_messages = self._load_transcript_messages(self._transcript)
        is_first = len(history_messages) == 0
        formatted_prompt = PROMPT_BUILDER.format_turn_prompt(
            prepared.agent_type,
            turn.content,
            is_first_turn=is_first,
        )
        if formatted_prompt:
            history_messages.append(
                PROMPT_BUILDER.to_langchain_message(
                    role=turn.role,
                    content=formatted_prompt,
                    message_id=turn.message_id,
                )
            )

        tool_factory = WorkflowDynamicToolFactory(task_id=self._task_id, run_id=run_id)
        dynamic_tools: list[BaseTool] = [tool_factory.mcp_tool(spec) for spec in prepared.mcp_tools]
        if prepared.slack_enabled:
            dynamic_tools.extend(
                tool_factory.slack_tools(
                    sandbox_provider=prepared.sandbox_provider,
                    sandbox_id=prepared.sandbox_id,
                )
            )

        middleware = WorkflowTurnMiddleware(
            task_id=self._task_id,
            run_id=run_id,
            model=prepared.model,
            slack_enabled=prepared.slack_enabled,
            extra_tools=dynamic_tools,
            drain_steering=self._drain_steering,
            is_cancelled=lambda: self._cancel_requested,
        )
        backend = self._select_backend(prepared)
        system_prompt = PROMPT_BUILDER.build_system_prompt(
            agent_type=prepared.agent_type,
            repo=prepared.repo,
            base_ref=prepared.base_ref,
            workspace_path=prepared.workspace_path,
            metadata=prepared.metadata,
            slack_instructions=prepared.slack_instructions,
        )
        encoded_model = PlatformModelSpec(
            model=prepared.model,
            effort=prepared.effort,
        ).encode()

        agent = create_temporal_deep_agent(
            model=encoded_model,
            system_prompt=system_prompt,
            tools=[PLAN_APPROVAL_TOOL, USER_GUIDANCE_TOOL],
            backend=backend,
            middleware=[middleware],
            interrupt_on={
                "request_plan_approval": {"allowed_decisions": ["approve", "reject"]},
                "request_user_guidance": {"allowed_decisions": ["respond"]},
            },
            checkpointer=InMemorySaver(),
            activity_options={
                "start_to_close_timeout": timedelta(minutes=10),
                "heartbeat_timeout": timedelta(seconds=60),
                "retry_policy": RetryPolicy(
                    maximum_attempts=4,
                    initial_interval=timedelta(seconds=2),
                    backoff_coefficient=2.0,
                ),
            },
        )
        runnable_cfg: RunnableConfig = {
            "recursion_limit": max(prepared.recursion_limit, 100),
            "configurable": {"thread_id": f"{self._task_id}:{run_id}"},
        }

        try:
            raw_result = await agent.ainvoke(
                {"messages": history_messages},
                config=runnable_cfg,
            )
            while isinstance(raw_result, dict) and raw_result.get("__interrupt__"):
                kind, prompt_text, approval_payload = self._extract_interrupt_details(
                    raw_result["__interrupt__"]
                )
                cur_msgs_raw = raw_result.get("messages")
                cur_msgs = (
                    [m for m in cur_msgs_raw if isinstance(m, BaseMessage)]
                    if isinstance(cur_msgs_raw, list)
                    else middleware.latest_messages
                )
                approval_id_raw = await workflow.execute_activity(
                    ENTER_PARKED_ACTIVITY,
                    EnterParkedInput(
                        task_id=self._task_id,
                        run_id=run_id,
                        reason=kind,
                        prompt=prompt_text,
                        approval_payload=approval_payload,
                        messages=self._dump_messages(cur_msgs),
                    ),
                    result_type=str,
                    start_to_close_timeout=timedelta(seconds=60),
                    summary=f"alephat:park:{kind}",
                )
                approval_id = approval_id_raw or None
                self._pending_approval = PendingApprovalView(
                    approval_id=approval_id,
                    run_id=run_id,
                    kind=kind,
                    prompt=prompt_text,
                )
                self._approval_resolution = None

                await workflow.wait_condition(
                    lambda: self._approval_resolution is not None or self._cancel_requested
                )
                resolution = self._approval_resolution
                self._pending_approval = None
                self._approval_resolution = None
                if self._cancel_requested or resolution is None:
                    raise asyncio.CancelledError("Task cancelled while parked")

                await workflow.execute_activity(
                    EXIT_PARKED_ACTIVITY,
                    ExitParkedInput(task_id=self._task_id, run_id=run_id),
                    start_to_close_timeout=timedelta(seconds=60),
                    summary="alephat:resume_parked",
                )
                if resolution.decision == "approved":
                    decision_obj: dict[str, object] = {"type": "approve"}
                elif resolution.decision == "rejected":
                    decision_obj = {
                        "type": "reject",
                        "message": resolution.comment or "Plan rejected; revise.",
                    }
                else:
                    decision_obj = {
                        "type": "respond",
                        "message": resolution.comment or "",
                    }
                raw_result = await agent.ainvoke(
                    Command(resume={"decisions": [decision_obj]}),
                    config=runnable_cfg,
                )
        except asyncio.CancelledError, CancelledError:
            dumped_partial = self._dump_messages(middleware.latest_messages or history_messages)
            self._transcript = dumped_partial
            await workflow.execute_activity(
                COMPLETE_TURN_ACTIVITY,
                CompleteTurnInput(
                    task_id=self._task_id,
                    run_id=run_id,
                    success=False,
                    cancelled=True,
                    final_message="cancelled",
                    error_code="cancelled",
                    sandbox_provider=prepared.sandbox_provider,
                    sandbox_id=prepared.sandbox_id,
                    repo=prepared.repo,
                    messages=dumped_partial,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                summary="alephat:complete_cancelled",
            )
            return
        except Exception as exc:
            dumped_err = self._dump_messages(middleware.latest_messages or history_messages)
            self._transcript = dumped_err
            await workflow.execute_activity(
                COMPLETE_TURN_ACTIVITY,
                CompleteTurnInput(
                    task_id=self._task_id,
                    run_id=run_id,
                    success=False,
                    cancelled=False,
                    final_message=str(exc),
                    error_code="agent_exception",
                    sandbox_provider=prepared.sandbox_provider,
                    sandbox_id=prepared.sandbox_id,
                    repo=prepared.repo,
                    messages=dumped_err,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                summary="alephat:complete_failed",
            )
            return

        out_msgs_raw = raw_result.get("messages") if isinstance(raw_result, dict) else None
        final_messages = (
            [m for m in out_msgs_raw if isinstance(m, BaseMessage)]
            if isinstance(out_msgs_raw, list)
            else (middleware.latest_messages or history_messages)
        )
        dumped_final = self._dump_messages(final_messages)
        self._transcript = dumped_final
        final_text = PROMPT_BUILDER.final_assistant_text(final_messages)

        leftover_steered = self._drain_steering()
        for item in leftover_steered:
            self._pending_turns.append(replace(item, run_id=item.run_id or str(workflow.uuid4())))

        await workflow.execute_activity(
            COMPLETE_TURN_ACTIVITY,
            CompleteTurnInput(
                task_id=self._task_id,
                run_id=run_id,
                success=True,
                cancelled=False,
                final_message=final_text,
                sandbox_provider=prepared.sandbox_provider,
                sandbox_id=prepared.sandbox_id,
                repo=prepared.repo,
                messages=dumped_final,
            ),
            start_to_close_timeout=timedelta(seconds=90),
            summary="alephat:complete_succeeded",
        )

    @workflow.run
    async def run(self, params: TaskWorkflowInput) -> dict[str, object]:
        del params
        while True:
            while self._pending_turns:
                next_turn = self._pending_turns.popleft()
                self._turn_in_progress = True
                turn_task = asyncio.create_task(self._run_single_turn(next_turn))
                self._current_turn_task = turn_task
                try:
                    await turn_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._current_turn_task = None
                    self._turn_in_progress = False
                    self._pending_approval = None

                if workflow.info().is_continue_as_new_suggested():
                    await self.stream.continue_as_new(
                        lambda stream_state: [
                            TaskWorkflowInput(
                                task_id=self._task_id,
                                org_id=self._org_id,
                                sandbox_id=self._sandbox_id,
                                transcript=list(self._transcript),
                                seen_turn_ids=list(self._seen_turn_ids),
                                pending_turns=list(self._pending_turns),
                                stream_state=stream_state,
                            )
                        ]
                    )

            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending_turns),
                    timeout=DEFAULT_IDLE_TIMEOUT,
                )
            except TimeoutError:
                if not self._pending_turns:
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    return {
                        "task_id": self._task_id,
                        "sandbox_id": self._sandbox_id,
                        "turns_completed": True,
                    }


@workflow.defn(name=SCHEDULE_WORKFLOW_NAME)
class ScheduledTaskWorkflow:
    """Thin workflow executed by Temporal Schedules to spawn scheduled coding tasks."""

    @workflow.run
    async def run(self, params: ScheduledAutomationInput) -> dict[str, str]:
        return await workflow.execute_activity(
            TRIGGER_SCHEDULED_AUTOMATION_ACTIVITY,
            params,
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
            summary=f"schedule:{params.schedule_id}",
        )
