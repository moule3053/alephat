"""Temporal Activity implementations for the Alephat platform worker."""

import base64
import inspect
import json
import logging
import shlex
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages import message_to_dict as langchain_message_to_dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from alephat_platform.common.config import Settings, get_settings
from alephat_platform.common.db import session_scope
from alephat_platform.common.enums import (
    ApprovalStatus,
    MessageKind,
    ParkReason,
    RunStatus,
    TaskStatus,
)
from alephat_platform.common.models import Approval, Message, OrgSettings, Run, SandboxRow, Task
from alephat_platform.common.slack import SlackClient
from alephat_platform.common.tasks import TaskDispatcher, TaskStore
from alephat_platform.common.temporal import (
    STANDALONE_SLACK_ACK_ACTIVITY,
    ScheduledAutomationInput,
    SlackAckInput,
    TemporalPlatformClient,
)
from alephat_platform.harness.agents import PROMPT_BUILDER
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
from alephat_platform.harness.mcp_runtime import MCP_RUNTIME
from alephat_platform.harness.sandboxes import (
    SANDBOX_REGISTRY,
    SandboxProviderBase,
    SandboxRef,
    get_sandbox_provider,
)
from alephat_platform.harness.slack import SlackReply, SlackTools
from openswe.github.app import get_github_app_installation_token

logger = logging.getLogger(__name__)


class RepositoryProvisioner:
    """Clone task repositories into sandboxes and inspect sandbox PR outcomes."""

    def __init__(
        self,
        token_fetcher: Callable[..., object] = get_github_app_installation_token,
    ) -> None:
        self._token_fetcher = token_fetcher

    def setup_command(self, repo: str, base_ref: str | None, token: str | None) -> str:
        owner, separator, repo_name = repo.partition("/")
        if not separator or not owner or not repo_name or "/" in repo_name:
            raise ValueError("Repository must use owner/name form")

        clone_url = f"https://github.com/{owner}/{repo_name}.git"
        ssh_url = f"git@github.com:{owner}/{repo_name}.git"
        branch = base_ref or "main"
        clone_config = ""
        configure_auth = "git config --unset-all http.https://github.com/.extraheader || true"
        if token:
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            header = f"Authorization: Basic {encoded}"
            clone_config = f"-c {shlex.quote(f'http.extraHeader={header}')} "
            configure_auth = "git config http.https://github.com/.extraheader " + shlex.quote(
                header
            )

        q_clone_url = shlex.quote(clone_url)
        q_ssh_url = shlex.quote(ssh_url)
        q_branch = shlex.quote(branch)
        return "\n".join(
            [
                "set -eu",
                "export GIT_TERMINAL_PROMPT=0",
                "rm -f .gitconfig-sandbox",
                "if [ -d .git ]; then",
                '  actual_origin="$(git remote get-url origin)"',
                f'  if [ "$actual_origin" != {q_clone_url} ] && [ "$actual_origin" != {q_ssh_url} ]; then',
                '    echo "Sandbox contains a different repository" >&2',
                "    exit 42",
                "  fi",
                'elif [ -n "$(find . -mindepth 1 -maxdepth 1 -print -quit)" ]; then',
                '  echo "Sandbox workspace is not empty" >&2',
                "  exit 43",
                "else",
                f"  git {clone_config}clone --branch {q_branch} --single-branch {q_clone_url} .",
                "fi",
                configure_auth,
                'git config user.name "$(git config user.name || echo Alephat)"',
                'git config user.email "$(git config user.email || echo alephat@users.noreply.github.com)"',
                "git rev-parse --show-toplevel >/dev/null",
            ]
        )

    async def prepare(
        self,
        provider: SandboxProviderBase,
        sandbox_ref: SandboxRef,
        *,
        repo: str | None,
        base_ref: str | None,
    ) -> None:
        if not repo:
            return
        _owner, _separator, repo_name = repo.partition("/")
        raw_token = self._token_fetcher(
            repositories=[repo_name] if repo_name else None,
            log_errors=False,
        )
        token_val = await raw_token if inspect.isawaitable(raw_token) else raw_token
        token = str(token_val) if isinstance(token_val, str) and token_val else None
        command = self.setup_command(repo, base_ref, token)
        result = await provider.exec(sandbox_ref, command, timeout=240)
        if result.exit_code != 0:
            lines = (result.stderr or result.stdout or "clone failed").strip().splitlines()
            detail = lines[-1] if lines else "clone failed"
            raise RuntimeError(f"Could not prepare {repo}: {detail[:300]}")

    async def collect_outcome(
        self,
        provider: SandboxProviderBase,
        sandbox_ref: SandboxRef,
        *,
        repo: str | None,
    ) -> dict[str, object]:
        if not repo:
            return {}
        script = "\n".join(
            [
                'branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"',
                'if [ -n "$branch" ] && [ "$branch" != "HEAD" ]; then',
                '  gh pr view "$branch" --json number,title,url,state,isDraft,headRefName,baseRefName,createdAt,additions,deletions,changedFiles 2>/dev/null || true',
                "fi",
            ]
        )
        try:
            res = await provider.exec(sandbox_ref, script, timeout=30)
        except Exception:
            logger.warning("Could not query sandbox PR status", exc_info=True)
            return {}
        raw = (res.stdout or "").strip()
        if not raw.startswith("{"):
            return {}
        try:
            pr = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(pr, dict) or not isinstance(pr.get("number"), int) or not pr.get("url"):
            return {}
        state_raw = str(pr.get("state") or "OPEN").lower()
        state = (
            "draft"
            if pr.get("isDraft") is True and state_raw == "open"
            else (
                "merged"
                if state_raw == "merged"
                else ("closed" if state_raw == "closed" else "open")
            )
        )
        additions = int(pr.get("additions") or 0)
        deletions = int(pr.get("deletions") or 0)
        changed_files = int(pr.get("changedFiles") or 0)
        head_ref = str(pr.get("headRefName") or "main")
        base_ref = str(pr.get("baseRefName") or "main")
        pr_record: dict[str, object] = {
            "number": pr["number"],
            "title": str(pr.get("title") or f"PR #{pr['number']}"),
            "state": state,
            "headRef": head_ref,
            "baseRef": base_ref,
            "url": str(pr["url"]),
            "repoFullName": repo,
            "author": "moule3053",
            "authorAvatarUrl": "https://github.com/moule3053.png",
            "createdAt": pr.get("createdAt"),
            "diffStats": {
                "files": changed_files,
                "additions": additions,
                "deletions": deletions,
            },
        }
        return {
            "pr_url": str(pr["url"]),
            "pr_number": pr["number"],
            "head_branch": head_ref,
            "additions": additions,
            "deletions": deletions,
            "pull_requests": [pr_record],
        }


class TranscriptProjector:
    """Sync LangChain messages between Temporal Workflow state and PostgreSQL ``messages``."""

    def message_uuid(
        self, task_id: uuid.UUID, raw_id: str | None, index: int, role: str
    ) -> uuid.UUID:
        if raw_id:
            try:
                return uuid.UUID(raw_id)
            except ValueError:
                return uuid.uuid5(task_id, f"{role}:{raw_id}")
        return uuid.uuid5(task_id, f"{role}:idx:{index}")

    def extract_text(self, content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)
        return str(content or "")

    def normalize_dumped_entry(
        self, raw: dict[str, object], index: int
    ) -> tuple[str, str | None, str, dict[str, object]]:
        kwargs_raw = raw.get("kwargs")
        data_raw = raw.get("data")
        inner = (
            kwargs_raw
            if isinstance(kwargs_raw, dict)
            else (data_raw if isinstance(data_raw, dict) else raw)
        )
        raw_id_list = raw.get("id")
        type_hint = (
            inner.get("type")
            or raw.get("type")
            or raw.get("role")
            or (raw_id_list[-1] if isinstance(raw_id_list, list) and raw_id_list else "")
        )
        lowered = str(type_hint).lower()
        if "tool" in lowered:
            role = "tool"
        elif "ai" in lowered or "assistant" in lowered or "agent" in lowered:
            role = "ai"
        elif "system" in lowered:
            role = "system"
        else:
            role = "human"

        msg_id_val = inner.get("id") or (raw.get("id") if isinstance(raw.get("id"), str) else None)
        msg_id = str(msg_id_val) if isinstance(msg_id_val, str) and msg_id_val else None
        content_raw = inner.get("content") if "content" in inner else raw.get("content", "")
        text = self.extract_text(content_raw)

        langgraph_dict: dict[str, object] = {
            "type": role,
            "data": {
                "id": msg_id or f"msg-{index}",
                "content": content_raw,
            },
        }
        data_section = langgraph_dict["data"]
        assert isinstance(data_section, dict)
        for key in (
            "tool_calls",
            "invalid_tool_calls",
            "tool_call_id",
            "name",
            "artifact",
            "additional_kwargs",
            "response_metadata",
            "usage_metadata",
        ):
            val = inner.get(key)
            if val is not None and val != [] and val != {}:
                data_section[key] = val
        return role, msg_id, text, langgraph_dict

    async def sync_to_db(
        self,
        session: AsyncSession,
        *,
        task_id: uuid.UUID,
        dumped_messages: list[dict[str, object]],
    ) -> None:
        if not dumped_messages:
            return
        existing_res = await session.execute(select(Message).where(Message.task_id == task_id))
        by_id = {m.message_id: m for m in existing_res.scalars().all()}

        for idx, entry in enumerate(dumped_messages):
            if not isinstance(entry, dict):
                continue
            role, raw_id, text, langgraph_dict = self.normalize_dumped_entry(entry, idx)
            if role == "system":
                continue
            target_id = self.message_uuid(task_id, raw_id, idx, role)
            existing = by_id.get(target_id)
            if role in {"human", "user"} and existing is not None:
                continue
            kind = (
                MessageKind.ASSISTANT.value
                if role == "ai"
                else (MessageKind.USER_INPUT.value if role == "human" else MessageKind.SYSTEM.value)
            )
            blocks: list[dict[str, object]] = [{"langgraph": langgraph_dict}]
            if existing is not None:
                existing.content = text
                existing.blocks = blocks
            else:
                created = Message(
                    message_id=target_id,
                    task_id=task_id,
                    role="user" if role == "human" else role,
                    kind=kind,
                    content=text,
                    blocks=blocks,
                )
                session.add(created)
                by_id[target_id] = created
        await session.flush()

    async def load_prior_dumped(
        self,
        session: AsyncSession,
        *,
        task_id: uuid.UUID,
        exclude_message_ids: set[str] | None = None,
    ) -> list[dict[str, object]]:
        excluded = exclude_message_ids or set()
        result = await session.execute(
            select(Message).where(Message.task_id == task_id).order_by(Message.seq)
        )
        rows = list(result.scalars().all())
        rebuilt: list[BaseMessage] = []
        for row in rows:
            if str(row.message_id) in excluded:
                continue
            lg_block = (
                row.blocks[0].get("langgraph")
                if row.blocks and isinstance(row.blocks[0], dict)
                else None
            )
            if isinstance(lg_block, dict):
                data = lg_block.get("data")
                d = data if isinstance(data, dict) else {}
                msg_type = str(lg_block.get("type") or row.role)
                msg_id = str(d.get("id") or row.message_id)
                content = d.get("content", row.content)
                if msg_type == "ai":
                    raw_tc = d.get("tool_calls")
                    tcs = list(raw_tc) if isinstance(raw_tc, list) else []
                    rebuilt.append(
                        AIMessage(
                            id=msg_id,
                            content=content if isinstance(content, str | list) else str(content),
                            tool_calls=tcs,
                        )
                    )
                    continue
                if msg_type == "tool":
                    tc_id = str(d.get("tool_call_id") or "")
                    if tc_id:
                        rebuilt.append(
                            ToolMessage(
                                id=msg_id,
                                content=str(content),
                                tool_call_id=tc_id,
                                name=str(d.get("name") or "tool"),
                            )
                        )
                    continue
            if row.role in {"assistant", "agent", "ai"}:
                rebuilt.append(AIMessage(id=str(row.message_id), content=row.content))
            elif row.role in {"user", "human", "user_guidance"}:
                rebuilt.append(HumanMessage(id=str(row.message_id), content=row.content))
        dumped_list: list[dict[str, object]] = []
        for message in rebuilt:
            raw_dump = dumpd(message)
            if isinstance(raw_dump, dict):
                dumped_list.append(raw_dump)
            else:
                dumped_list.append(langchain_message_to_dict(message))
        return dumped_list


class PlatformActivities:
    """Bound Temporal Activity methods executing DB, sandbox, GitHub, Slack, and MCP I/O."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = self.settings.worker_id
        self.provisioner = RepositoryProvisioner()
        self.projector = TranscriptProjector()

    def activity_list(self) -> list[Callable[..., object]]:
        return [
            self.prepare_turn,
            self.record_step,
            self.enter_parked_state,
            self.exit_parked_state,
            self.complete_turn,
            self.invoke_mcp_tool,
            self.render_slack_card,
            self.upload_slack_file,
            self.acknowledge_slack_message,
            self.trigger_scheduled_automation,
        ]

    @activity.defn(name=PREPARE_TURN_ACTIVITY)
    async def prepare_turn(self, req: PrepareTurnInput) -> PreparedTurn:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)

        async with session_scope() as session:
            store = TaskStore(session)
            task = await store.get_task(task_id)
            PROMPT_BUILDER.validate_agent_type(task.agent_type)

            if req.message_ts:
                task.metadata_ = {**(task.metadata_ or {}), "message_ts": req.message_ts}

            prev_runs = await session.execute(
                select(Run).where(Run.task_id == task_id).order_by(Run.attempt.desc()).limit(1)
            )
            prev = prev_runs.scalar_one_or_none()
            existing_run = await session.get(Run, run_id)
            if existing_run is None:
                attempt = (prev.attempt + 1) if prev else 1
                existing_run = Run(
                    run_id=run_id,
                    task_id=task_id,
                    temporal_run_id=req.temporal_run_id,
                    status=RunStatus.ACTIVE.value,
                    attempt=attempt,
                    worker_id=self.worker_id,
                    started_at=datetime.now(UTC),
                )
                session.add(existing_run)
            else:
                existing_run.status = RunStatus.ACTIVE.value
                existing_run.temporal_run_id = req.temporal_run_id
                existing_run.worker_id = self.worker_id
                existing_run.updated_at = datetime.now(UTC)

            task.status = TaskStatus.RUNNING.value
            task.active_run_id = run_id
            task.park_reason = None
            task.updated_at = datetime.now(UTC)

            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type="run_started",
                payload={"worker_id": self.worker_id, "attempt": existing_run.attempt},
            )

            existing_sb_id = req.sandbox_id
            if not existing_sb_id:
                sb_res = await session.execute(
                    select(SandboxRow)
                    .where(
                        SandboxRow.task_id == task_id,
                        SandboxRow.provider == task.sandbox_provider,
                        SandboxRow.status == "active",
                    )
                    .order_by(SandboxRow.created_at.desc())
                    .limit(1)
                )
                sb_row = sb_res.scalar_one_or_none()
                if sb_row is not None:
                    existing_sb_id = sb_row.sandbox_id

            prior_messages = (
                await self.projector.load_prior_dumped(session, task_id=task_id)
                if req.load_history
                else []
            )
            snapshot_agent_type = task.agent_type
            snapshot_repo = task.repo
            snapshot_base_ref = task.base_ref
            snapshot_model = task.model
            snapshot_provider = task.sandbox_provider
            snapshot_mcp = [
                dict(item) for item in (task.mcp_snapshot or []) if isinstance(item, dict)
            ]
            snapshot_meta = dict(task.metadata_ or {})

        slack_reply = SlackReply(task_id, run_id, snapshot_meta)
        await slack_reply.start()
        slack_instructions = await slack_reply.instructions() if slack_reply.enabled else ""

        activity.heartbeat("provisioning_sandbox")
        provider = get_sandbox_provider(snapshot_provider)
        try:
            if existing_sb_id:
                try:
                    sandbox_ref = await provider.connect(existing_sb_id)
                    sb_event = "sandbox_reused"
                except Exception:
                    logger.warning(
                        "Sandbox reconnect failed; creating replacement",
                        extra={"sandbox_id": existing_sb_id},
                        exc_info=True,
                    )
                    sandbox_ref = await provider.create(task_id=str(task_id))
                    sb_event = "sandbox_created"
            else:
                sandbox_ref = await provider.create(task_id=str(task_id))
                sb_event = "sandbox_created"
        except Exception as exc:
            async with session_scope() as session:
                await TaskStore(session).append_event(
                    task_id=task_id,
                    run_id=run_id,
                    event_type="sandbox_error",
                    payload={"provider": snapshot_provider, "error": str(exc)},
                )
            raise ApplicationError(
                f"Sandbox unavailable ({snapshot_provider}): {exc}",
                type="SandboxUnavailableError",
                non_retryable=True,
            ) from exc

        async with session_scope() as session:
            store = TaskStore(session)
            await store.upsert_sandbox(
                task_id=task_id,
                provider=sandbox_ref.provider,
                sandbox_id=sandbox_ref.sandbox_id,
                metadata=sandbox_ref.metadata,
            )
            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type=sb_event,
                payload={"sandbox_id": sandbox_ref.sandbox_id, "provider": sandbox_ref.provider},
            )

        if snapshot_repo:
            activity.heartbeat("cloning_repository")
            try:
                await self.provisioner.prepare(
                    provider,
                    sandbox_ref,
                    repo=snapshot_repo,
                    base_ref=snapshot_base_ref,
                )
            except Exception as exc:
                is_transient = isinstance(exc, (httpx.HTTPError, TimeoutError, OSError))
                attempt = activity.info().attempt if activity.in_activity() else 3
                if not is_transient or attempt >= 3:
                    async with session_scope() as session:
                        await TaskStore(session).append_event(
                            task_id=task_id,
                            run_id=run_id,
                            event_type="repository_error",
                            payload={"repo": snapshot_repo, "error": str(exc)},
                        )
                raise ApplicationError(
                    str(exc),
                    type="RepositorySetupError",
                    non_retryable=not is_transient,
                ) from exc
            async with session_scope() as session:
                await TaskStore(session).append_event(
                    task_id=task_id,
                    run_id=run_id,
                    event_type="repository_prepared",
                    payload={"repo": snapshot_repo, "base_ref": snapshot_base_ref},
                )

        mcp_wire_specs: list[McpToolWireSpec] = []
        if snapshot_mcp:
            activity.heartbeat("connecting_mcp")
            try:
                mcp_sessions, specs = await MCP_RUNTIME.discover_specs(snapshot_mcp)
            except Exception as exc:
                async with session_scope() as session:
                    await TaskStore(session).append_event(
                        task_id=task_id,
                        run_id=run_id,
                        event_type="mcp_error",
                        payload={"error": str(exc), "required": True},
                    )
                raise ApplicationError(
                    f"Required MCP server failed: {exc}",
                    type="RequiredMcpError",
                    non_retryable=True,
                ) from exc
            async with session_scope() as session:
                store = TaskStore(session)
                for s in mcp_sessions:
                    if s.ok:
                        await store.append_event(
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_connected",
                            payload={
                                "mcp_server_id": s.server_id,
                                "name": s.name,
                                "tools": [t.tool_name for t in s.tools],
                            },
                        )
                        await store.append_event(
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_tools_loaded",
                            payload={
                                "mcp_server_id": s.server_id,
                                "tools": [t.namespaced for t in s.tools],
                            },
                        )
                    else:
                        await store.append_event(
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_error",
                            payload={"mcp_server_id": s.server_id, "error": s.error},
                        )
            mcp_wire_specs = [
                McpToolWireSpec(
                    namespaced_name=sp.namespaced_name,
                    server_id=sp.server_id,
                    server_name=sp.server_name,
                    tool_name=sp.tool_name,
                    description=sp.description,
                    args_schema=sp.args_schema,
                    server_config=sp.server_config,
                )
                for sp in specs
            ]

        raw_effort = snapshot_meta.get("agent_effort")
        effort = str(raw_effort) if isinstance(raw_effort, str) and raw_effort else None
        workspace_attr = getattr(sandbox_ref.handle, "workspace", None)
        workspace_path = str(workspace_attr) if isinstance(workspace_attr, str) else None
        is_stub = bool(sandbox_ref.metadata.get("stub") or sandbox_ref.handle is None)

        return PreparedTurn(
            task_id=str(task_id),
            run_id=str(run_id),
            agent_type=snapshot_agent_type,
            repo=snapshot_repo,
            base_ref=snapshot_base_ref,
            model=snapshot_model,
            effort=effort,
            sandbox_provider=sandbox_ref.provider,
            sandbox_id=sandbox_ref.sandbox_id,
            sandbox_is_stub=is_stub,
            workspace_path=workspace_path,
            metadata=snapshot_meta,
            slack_enabled=slack_reply.enabled,
            slack_instructions=slack_instructions,
            mcp_tools=mcp_wire_specs,
            prior_messages=prior_messages,
        )

    @activity.defn(name=RECORD_STEP_ACTIVITY)
    async def record_step(self, req: RecordStepInput) -> None:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        meta: dict[str, object] = {}
        async with session_scope() as session:
            store = TaskStore(session)
            task = await session.get(Task, task_id)
            if task is None:
                return
            meta = dict(task.metadata_ or {})
            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type=req.event_type,
                payload=req.payload,
            )
            if req.messages:
                await self.projector.sync_to_db(
                    session, task_id=task_id, dumped_messages=req.messages
                )
        if req.preview_text:
            reply = SlackReply(task_id, run_id, meta)
            await reply.preview(req.preview_text)

    @activity.defn(name=ENTER_PARKED_ACTIVITY)
    async def enter_parked_state(self, req: EnterParkedInput) -> str:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        approval_id_str = ""
        meta: dict[str, object] = {}

        async with session_scope() as session:
            store = TaskStore(session)
            task = await store.get_task(task_id)
            meta = dict(task.metadata_ or {})
            run = await session.get(Run, run_id)
            if req.messages:
                await self.projector.sync_to_db(
                    session, task_id=task_id, dumped_messages=req.messages
                )

            reason = ParkReason(req.reason).value
            task.status = TaskStatus.PARKED.value
            task.park_reason = reason
            task.updated_at = datetime.now(UTC)
            if run is not None:
                run.status = RunStatus.PARKED.value
                run.worker_id = None
                run.updated_at = datetime.now(UTC)

            if ParkReason(reason) == ParkReason.PLAN_APPROVAL:
                approval = Approval(
                    task_id=task_id,
                    run_id=run_id,
                    kind="plan_approval",
                    payload=req.approval_payload,
                    status=ApprovalStatus.PENDING.value,
                )
                session.add(approval)
                await session.flush()
                approval_id_str = str(approval.approval_id)
                await store.append_event(
                    task_id=task_id,
                    run_id=run_id,
                    event_type="plan_proposed",
                    payload={"approval_id": approval_id_str},
                )
            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type="parked",
                payload={"reason": reason},
            )

        reply = SlackReply(task_id, run_id, meta)
        await reply.finish(req.prompt, status=req.reason)
        return approval_id_str

    @activity.defn(name=EXIT_PARKED_ACTIVITY)
    async def exit_parked_state(self, req: ExitParkedInput) -> None:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        meta: dict[str, object] = {}
        async with session_scope() as session:
            store = TaskStore(session)
            task = await store.get_task(task_id)
            meta = dict(task.metadata_ or {})
            task.status = TaskStatus.RUNNING.value
            task.park_reason = None
            task.updated_at = datetime.now(UTC)
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.ACTIVE.value
                run.worker_id = self.worker_id
                run.updated_at = datetime.now(UTC)
            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type="run_resumed",
                payload={"worker_id": self.worker_id},
            )
        reply = SlackReply(task_id, run_id, meta)
        if reply.enabled:
            try:
                await reply.publish(markdown="Working on it…", controls="running")
            except Exception:
                logger.warning("Could not refresh Slack resumed controls", exc_info=True)

    @activity.defn(name=COMPLETE_TURN_ACTIVITY)
    async def complete_turn(self, req: CompleteTurnInput) -> None:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)

        repo_outcome: dict[str, object] = {}
        if req.success and not req.cancelled and req.repo and req.sandbox_id:
            try:
                provider = get_sandbox_provider(req.sandbox_provider)
                ref = await provider.connect(req.sandbox_id)
                repo_outcome = await self.provisioner.collect_outcome(provider, ref, repo=req.repo)
            except Exception:
                logger.warning("Could not collect sandbox repository outcome", exc_info=True)

        meta: dict[str, object] = {}
        reply_status = TaskStatus.SUCCEEDED.value
        async with session_scope() as session:
            store = TaskStore(session)
            task = await store.get_task(task_id)
            run = await session.get(Run, run_id)
            if req.messages:
                await self.projector.sync_to_db(
                    session, task_id=task_id, dumped_messages=req.messages
                )
            if repo_outcome:
                task.metadata_ = {**(task.metadata_ or {}), **repo_outcome}
            meta = dict(task.metadata_ or {})

            is_cancelled = req.cancelled or bool(task.cancel_requested_at)
            target = (
                TaskStatus.CANCELLED
                if is_cancelled
                else (TaskStatus.SUCCEEDED if req.success else TaskStatus.FAILED)
            )
            task.status = target.value
            task.park_reason = None
            task.updated_at = datetime.now(UTC)
            reply_status = target.value

            error_code = (
                req.error_code or "cancelled" if target == TaskStatus.CANCELLED else req.error_code
            )
            error_message = None if target == TaskStatus.SUCCEEDED else req.final_message

            if run is not None:
                run.status = (
                    RunStatus.CANCELLED.value
                    if target == TaskStatus.CANCELLED
                    else (
                        RunStatus.SUCCEEDED.value
                        if target == TaskStatus.SUCCEEDED
                        else RunStatus.FAILED.value
                    )
                )
                run.ended_at = datetime.now(UTC)
                run.worker_id = None
                run.error_code = error_code
                run.error_message = error_message

            event_type = (
                "cancelled"
                if target == TaskStatus.CANCELLED
                else ("run_succeeded" if target == TaskStatus.SUCCEEDED else "run_failed")
            )
            await store.append_event(
                task_id=task_id,
                run_id=run_id,
                event_type=event_type,
                payload={"error_code": error_code, "error_message": error_message},
            )

        pr_url_val = repo_outcome.get("pr_url")
        pr_url = str(pr_url_val) if isinstance(pr_url_val, str) and pr_url_val else None
        reply = SlackReply(task_id, run_id, meta)
        await reply.finish(
            req.final_message or "Task completed.",
            status=reply_status,
            pr_url=pr_url,
        )

    @activity.defn(name=INVOKE_MCP_TOOL_ACTIVITY)
    async def invoke_mcp_tool(self, req: InvokeMcpToolInput) -> str:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        payload: dict[str, object] = {
            "tool": req.namespaced_name,
            "mcp_server_id": req.server_id,
        }
        async with session_scope() as session:
            await TaskStore(session).append_event(
                task_id=task_id,
                run_id=run_id,
                event_type="tool_started",
                payload=payload,
            )
        try:
            output = await MCP_RUNTIME.invoke_tool(
                server_config=req.server_config,
                tool_name=req.tool_name,
                args=req.args,
            )
        except Exception as exc:
            async with session_scope() as session:
                await TaskStore(session).append_event(
                    task_id=task_id,
                    run_id=run_id,
                    event_type="tool_finished",
                    payload={**payload, "ok": False, "error": str(exc)},
                )
            raise
        async with session_scope() as session:
            await TaskStore(session).append_event(
                task_id=task_id,
                run_id=run_id,
                event_type="tool_finished",
                payload={**payload, "ok": True},
            )
        return output

    @activity.defn(name=RENDER_SLACK_CARD_ACTIVITY)
    async def render_slack_card(self, req: RenderSlackCardInput) -> dict[str, object]:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        async with session_scope() as session:
            task = await TaskStore(session).get_task(task_id)
            meta = dict(task.metadata_ or {})
        reply = SlackReply(task_id, run_id, meta)
        tools = SlackTools(reply, SandboxRef(provider="local", sandbox_id="none"))
        result = await tools.render_card(req.spec_json, req.fallback_text)
        return dict(result)

    @activity.defn(name=UPLOAD_SLACK_FILE_ACTIVITY)
    async def upload_slack_file(self, req: UploadSlackFileInput) -> dict[str, object]:
        task_id = uuid.UUID(req.task_id)
        run_id = uuid.UUID(req.run_id)
        async with session_scope() as session:
            task = await TaskStore(session).get_task(task_id)
            meta = dict(task.metadata_ or {})
        provider = SANDBOX_REGISTRY.get(req.sandbox_provider)
        ref = await provider.connect(req.sandbox_id)
        if ref.handle is None:
            ref = SandboxRef(
                provider=req.sandbox_provider,
                sandbox_id=req.sandbox_id,
                handle=SANDBOX_REGISTRY.stub_backend(req.sandbox_id),
            )
        reply = SlackReply(task_id, run_id, meta)
        tools = SlackTools(reply, ref)
        result = await tools.upload_file(req.path, req.title)
        return dict(result)

    @activity.defn(name=STANDALONE_SLACK_ACK_ACTIVITY)
    async def acknowledge_slack_message(self, req: SlackAckInput) -> None:
        token = self.settings.slack_bot_token
        if not token or not req.channel or not req.message_ts:
            return
        await SlackClient(token).react(req.channel, req.message_ts, "eyes")

    @activity.defn(name=TRIGGER_SCHEDULED_AUTOMATION_ACTIVITY)
    async def trigger_scheduled_automation(self, req: ScheduledAutomationInput) -> dict[str, str]:
        org_id = uuid.UUID(req.org_id)
        user_id = uuid.UUID(req.user_id)
        temporal = TemporalPlatformClient(settings=self.settings)

        async with session_scope() as session:
            org = await session.get(OrgSettings, org_id)
            raw_schedules = (org.settings or {}).get("schedules") if org else None
            schedules = (
                {str(k): dict(v) for k, v in raw_schedules.items() if isinstance(v, dict)}
                if isinstance(raw_schedules, dict)
                else {}
            )
            record = schedules.get(req.schedule_id)
            if record is None or record.get("enabled") is False:
                return {"status": "skipped", "schedule_id": req.schedule_id}

            triggers = record.get("triggers")
            repo = next(
                (
                    str(t.get("repo"))
                    for t in (triggers if isinstance(triggers, list) else [])
                    if isinstance(t, dict) and isinstance(t.get("repo"), str) and t.get("repo")
                ),
                None,
            )
            thread_id = str(uuid.uuid4())
            model_val = record.get("model")
            dispatcher = TaskDispatcher(session, temporal)
            task = await dispatcher.create_and_start(
                org_id=org_id,
                user_id=user_id,
                title=str(record.get("name") or "Scheduled Run")[:100],
                prompt=str(record.get("prompt") or ""),
                repo=repo,
                base_ref="main",
                source="web_ui",
                source_ref=req.schedule_id,
                thread_id=thread_id,
                agent_type="coding",
                model=str(model_val) if isinstance(model_val, str) and model_val else None,
                metadata={
                    "schedule_id": req.schedule_id,
                    "agent_effort": record.get("effort"),
                    "experience": "coding" if repo else "agent_workspace",
                },
                platform_default_model=self.settings.default_model,
                platform_default_sandbox_provider=self.settings.default_sandbox_provider,
            )
            now = datetime.now(UTC).isoformat()
            run_id_str = str(task.active_run_id or task.task_id)
            assert org is not None
            settings_dict = dict(org.settings or {})
            record["lastThreadId"] = thread_id
            record["lastRunId"] = run_id_str
            record["lastTriggeredAt"] = now
            record["updatedAt"] = now
            schedules[req.schedule_id] = record
            settings_dict["schedules"] = schedules
            org.settings = settings_dict
            org.updated_at = datetime.now(UTC)

        return {
            "status": "started",
            "schedule_id": req.schedule_id,
            "thread_id": thread_id,
            "run_id": run_id_str,
        }
