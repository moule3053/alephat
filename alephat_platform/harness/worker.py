"""Harness worker main loop: consume NATS TASKS, claim, run agent, checkpoint."""

import asyncio
import base64
import json
import logging
import shlex
import signal
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from alephat_platform.common.config import get_settings
from alephat_platform.common.db import dispose_engine, get_session_factory
from alephat_platform.common.enums import ParkReason, TaskStatus
from alephat_platform.common.messaging import SUBJECT_ENQUEUE_PREFIX, NatsBus
from alephat_platform.common.models import Message, ProcessedMessage, SandboxRow, Task
from alephat_platform.common.outbox import enqueue_task_work
from alephat_platform.common.tasks import (
    append_event,
    claim_task,
    complete_task,
    heartbeat_lease,
    load_latest_checkpoint,
    park_task,
    requeue_expired_leases,
    save_checkpoint,
    upsert_sandbox,
)
from alephat_platform.harness.agents import RunContext, get_agent
from alephat_platform.harness.llm_client import make_llm_client
from alephat_platform.harness.mcp_runtime import connect_mcp_servers
from alephat_platform.harness.sandboxes import SandboxProviderBase, SandboxRef, get_sandbox_provider
from alephat_platform.harness.slack import SlackReply
from openswe.github.app import get_github_app_installation_token

logger = logging.getLogger(__name__)


def _message_input(message: Message) -> dict[str, str]:
    return {
        "role": message.role,
        "content": message.content,
        "id": str(message.message_id),
    }


def _repository_setup_command(repo: str, base_ref: str | None, token: str | None) -> str:
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
        configure_auth = "git config http.https://github.com/.extraheader " + shlex.quote(header)

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


async def _collect_repo_outcome(
    provider: SandboxProviderBase,
    sandbox_ref: SandboxRef,
    *,
    repo: str | None,
) -> dict[str, Any]:
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
            "merged" if state_raw == "merged" else ("closed" if state_raw == "closed" else "open")
        )
    )
    additions = int(pr.get("additions") or 0)
    deletions = int(pr.get("deletions") or 0)
    changed_files = int(pr.get("changedFiles") or 0)
    head_ref = str(pr.get("headRefName") or "main")
    base_ref = str(pr.get("baseRefName") or "main")
    pr_record = {
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


async def _prepare_task_repository(
    provider: SandboxProviderBase,
    sandbox_ref: SandboxRef,
    *,
    repo: str | None,
    base_ref: str | None,
) -> None:
    if not repo:
        return
    _owner, _separator, repo_name = repo.partition("/")
    token = await get_github_app_installation_token(
        repositories=[repo_name] if repo_name else None,
        log_errors=False,
    )
    command = _repository_setup_command(repo, base_ref, token)
    result = await provider.exec(sandbox_ref, command, timeout=240)
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "clone failed").strip().splitlines()[-1]
        raise RuntimeError(f"Could not prepare {repo}: {detail[:300]}")


class HarnessWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = self.settings.worker_id
        self.bus = NatsBus(self.settings.nats_url)
        self.llm = make_llm_client()
        self._stop = asyncio.Event()
        self._slots = asyncio.Semaphore(4)
        self._running: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        self._stop.set()

    async def start(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [harness] %(message)s",
        )
        await self.bus.connect()
        assert self.bus.js is not None

        # Durable pull consumer on work queue
        try:
            await self.bus.js.add_consumer(
                "TASKS",
                durable_name="harness-workers",
                filter_subject=f"{SUBJECT_ENQUEUE_PREFIX}.>",
            )
        except Exception:
            # may already exist
            pass

        reaper = asyncio.create_task(self._reaper_loop())
        logger.info("Harness worker %s started", self.worker_id)
        try:
            await self._consume_loop()
        finally:
            self._stop.set()
            reaper.cancel()
            if self._running:
                await asyncio.gather(*self._running, return_exceptions=True)
            await self.bus.close()
            await dispose_engine()

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                factory = get_session_factory()
                async with factory() as session:
                    n = await requeue_expired_leases(
                        session, max_retries=self.settings.max_run_retries
                    )
                    await session.commit()
                    if n:
                        logger.warning("Reaped %s expired leases", n)
            except Exception:
                logger.exception("Reaper failed")
            await asyncio.sleep(self.settings.lease_ttl_seconds)

    async def _consume_loop(self) -> None:
        assert self.bus.js is not None
        psub = await self.bus.js.pull_subscribe(
            f"{SUBJECT_ENQUEUE_PREFIX}.>",
            durable="harness-workers",
            stream="TASKS",
        )
        while not self._stop.is_set():
            if self._slots.locked():
                await asyncio.sleep(0.2)
                continue
            try:
                msgs = await psub.fetch(1, timeout=2)
            except Exception:
                continue
            for msg in msgs:
                try:
                    body = json.loads(msg.data.decode())
                    claimed = await self._claim_enqueue(body)
                    await msg.ack()
                    if claimed is not None:
                        task_id, run_id = claimed
                        await self._slots.acquire()
                        run_task = asyncio.create_task(self._run_slot(task_id, run_id))
                        self._running.add(run_task)
                        run_task.add_done_callback(self._running.discard)
                except Exception:
                    logger.exception("Failed handling message")
                    try:
                        await msg.nak()
                    except Exception:
                        pass

    async def _run_slot(self, task_id: uuid.UUID, run_id: uuid.UUID) -> None:
        try:
            await self._execute_run(task_id, run_id)
        except Exception:
            logger.exception("Unhandled error in task %s run %s", task_id, run_id)
        finally:
            self._slots.release()

    async def _claim_enqueue(self, body: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID] | None:
        payload = body.get("payload") or body
        task_id_raw = payload.get("task_id") or body.get("task_id")
        if not task_id_raw:
            return None
        task_id = uuid.UUID(str(task_id_raw))
        msg_id = str(body.get("msg_id") or "")

        factory = get_session_factory()
        async with factory() as session:
            if msg_id and await session.get(ProcessedMessage, msg_id):
                return None
            claimed = await claim_task(
                session,
                task_id=task_id,
                worker_id=self.worker_id,
                lease_ttl_seconds=self.settings.lease_ttl_seconds,
            )
            if msg_id:
                session.add(ProcessedMessage(msg_id=msg_id))
            await session.commit()
            if claimed is None:
                logger.info("Task %s not claimable; acking", task_id)
                return None
            _, run = claimed
            return task_id, run.run_id

    async def _execute_run(self, task_id: uuid.UUID, run_id: uuid.UUID) -> None:
        factory = get_session_factory()
        async with factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            cp = await load_latest_checkpoint(session, task_id=task_id)
            checkpoint_last_seq = int((cp.blob if cp else {}).get("last_message_seq", 0))
            msg_result = await session.execute(
                select(Message)
                .where(Message.task_id == task_id, Message.seq > checkpoint_last_seq)
                .order_by(Message.seq)
            )
            message_rows = list(msg_result.scalars().all())
            messages = [_message_input(message) for message in message_rows]
            last_message_seq = max(
                [checkpoint_last_seq, *(int(m.seq) for m in message_rows)],
            )
            # existing sandbox?
            sb_result = await session.execute(
                select(SandboxRow)
                .where(
                    SandboxRow.task_id == task_id,
                    SandboxRow.provider == task.sandbox_provider,
                    SandboxRow.status == "active",
                )
                .order_by(SandboxRow.created_at.desc())
                .limit(1)
            )
            existing_sb = sb_result.scalar_one_or_none()
            task_snapshot = {
                "org_id": str(task.org_id),
                "task_id": str(task.task_id),
                "run_id": str(run_id),
                "thread_id": task.thread_id,
                "agent_type": task.agent_type,
                "repo": task.repo,
                "base_ref": task.base_ref,
                "model": task.model,
                "prompt": task.prompt,
                "messages": messages,
                "sandbox_provider": task.sandbox_provider,
                "sandbox_id": existing_sb.sandbox_id if existing_sb else None,
                "mcp_snapshot": list(task.mcp_snapshot or []),
                "checkpoint": cp.blob if cp else None,
                "metadata": dict(task.metadata_ or {}),
                "last_message_seq": last_message_seq,
                "cancel_requested": bool(task.cancel_requested_at),
            }
            await session.commit()

        slack_reply = SlackReply(task_id, run_id, task_snapshot["metadata"])
        await slack_reply.start()
        provider = get_sandbox_provider(task_snapshot["sandbox_provider"])
        try:
            if task_snapshot["sandbox_id"]:
                try:
                    sandbox_ref = await provider.connect(task_snapshot["sandbox_id"])
                    event_type = "sandbox_reused"
                except Exception:
                    logger.warning(
                        "Sandbox reconnect failed; creating a replacement", exc_info=True
                    )
                    sandbox_ref = await provider.create(task_id=str(task_id))
                    event_type = "sandbox_created"
            else:
                sandbox_ref = await provider.create(task_id=str(task_id))
                event_type = "sandbox_created"
        except Exception as exc:
            async with factory() as session:
                await append_event(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    event_type="sandbox_error",
                    payload={"provider": task_snapshot["sandbox_provider"], "error": str(exc)},
                )
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=False,
                    error_code="sandbox_unavailable",
                    error_message=str(exc),
                )
                await session.commit()
            await slack_reply.finish(
                "The sandbox could not be started. Please retry or check the sandbox configuration.",
                status="failed",
            )
            return

        async with factory() as session:
            await upsert_sandbox(
                session,
                task_id=task_id,
                provider=sandbox_ref.provider,
                sandbox_id=sandbox_ref.sandbox_id,
                metadata=sandbox_ref.metadata,
            )
            await append_event(
                session,
                task_id=task_id,
                run_id=run_id,
                event_type=event_type,
                payload={"sandbox_id": sandbox_ref.sandbox_id, "provider": sandbox_ref.provider},
            )
            await session.commit()

        try:
            await _prepare_task_repository(
                provider,
                sandbox_ref,
                repo=task_snapshot["repo"],
                base_ref=task_snapshot["base_ref"],
            )
        except Exception as exc:
            logger.warning("Repository setup failed for task %s: %s", task_id, exc)
            async with factory() as session:
                await append_event(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    event_type="repository_error",
                    payload={"repo": task_snapshot["repo"], "error": str(exc)},
                )
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=False,
                    error_code="repository_setup_failed",
                    error_message=str(exc),
                )
                await session.commit()
            await slack_reply.finish(
                "The repository could not be prepared. Please check repository access and the selected branch.",
                status="failed",
            )
            return

        if task_snapshot["repo"]:
            async with factory() as session:
                await append_event(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    event_type="repository_prepared",
                    payload={
                        "repo": task_snapshot["repo"],
                        "base_ref": task_snapshot["base_ref"],
                    },
                )
                await session.commit()

        # MCP
        mcp_sessions = []
        try:
            mcp_sessions = await connect_mcp_servers(task_snapshot["mcp_snapshot"])
            async with factory() as session:
                for s in mcp_sessions:
                    if s.ok:
                        await append_event(
                            session,
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_connected",
                            payload={
                                "mcp_server_id": s.server_id,
                                "name": s.name,
                                "tools": [t.tool_name for t in s.tools],
                            },
                        )
                        await append_event(
                            session,
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_tools_loaded",
                            payload={
                                "mcp_server_id": s.server_id,
                                "tools": [t.namespaced for t in s.tools],
                            },
                        )
                    else:
                        await append_event(
                            session,
                            task_id=task_id,
                            run_id=run_id,
                            event_type="mcp_error",
                            payload={"mcp_server_id": s.server_id, "error": s.error},
                        )
                await session.commit()
        except Exception as exc:
            async with factory() as session:
                await append_event(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    event_type="mcp_error",
                    payload={"error": str(exc), "required": True},
                )
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=False,
                    error_code="mcp_required_failed",
                    error_message=str(exc),
                )
                await session.commit()
            await slack_reply.finish(
                "A required integration could not be connected. Please check its configuration.",
                status="failed",
            )
            return

        ctx = RunContext(
            org_id=task_snapshot["org_id"],
            task_id=task_snapshot["task_id"],
            run_id=task_snapshot["run_id"],
            thread_id=task_snapshot["thread_id"],
            agent_type=task_snapshot["agent_type"],
            repo=task_snapshot["repo"],
            base_ref=task_snapshot["base_ref"],
            model=task_snapshot["model"],
            prompt=task_snapshot["prompt"],
            messages=task_snapshot["messages"],
            sandbox_provider=task_snapshot["sandbox_provider"],
            sandbox_id=sandbox_ref.sandbox_id,
            mcp_snapshot=task_snapshot["mcp_snapshot"],
            checkpoint=task_snapshot["checkpoint"],
            metadata=task_snapshot["metadata"],
            last_message_seq=task_snapshot["last_message_seq"],
            cancel_requested=task_snapshot["cancel_requested"],
            slack_reply=slack_reply,
        )

        try:
            agent = get_agent(ctx.agent_type)
        except KeyError:
            async with factory() as session:
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=False,
                    error_code="unknown_agent_type",
                    error_message=ctx.agent_type,
                )
                await session.commit()
            await slack_reply.finish("This task's agent type is not available.", status="failed")
            return

        async def on_step(event_type: str, payload: dict[str, Any]) -> None:
            async with factory() as session:
                if event_type == "checkpoint":
                    await save_checkpoint(
                        session,
                        task_id=task_id,
                        run_id=run_id,
                        blob={**payload, "last_message_seq": ctx.last_message_seq},
                    )
                else:
                    await append_event(
                        session,
                        task_id=task_id,
                        run_id=run_id,
                        event_type=event_type,
                        payload=payload,
                    )
                await session.commit()

        async def on_boundary() -> list[dict[str, str]]:
            async with factory() as session:
                task_row = await session.get(Task, task_id)
                if task_row and task_row.cancel_requested_at:
                    ctx.cancel_requested = True
                lease_ok = await heartbeat_lease(
                    session,
                    run_id=run_id,
                    worker_id=self.worker_id,
                    lease_ttl_seconds=self.settings.lease_ttl_seconds,
                )
                if not lease_ok:
                    raise RuntimeError("Worker lease was lost")
                pending = await session.execute(
                    select(Message)
                    .where(Message.task_id == task_id, Message.seq > ctx.last_message_seq)
                    .order_by(Message.seq)
                )
                rows = list(pending.scalars().all())
                if rows:
                    ctx.last_message_seq = max(int(row.seq) for row in rows)
                    new_ts = (task_row.metadata_ or {}).get("message_ts") if task_row else None
                    if isinstance(new_ts, str) and new_ts and new_ts != slack_reply.message_ts:
                        slack_reply.message_ts = new_ts
                        slack_reply.timestamps = []
                        slack_reply.spec = None
                await session.commit()
                return [_message_input(row) for row in rows]

        heartbeat_stop = asyncio.Event()

        async def heartbeat_loop() -> None:
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(
                        heartbeat_stop.wait(),
                        timeout=self.settings.heartbeat_interval_seconds,
                    )
                    return
                except TimeoutError:
                    pass
                async with factory() as session:
                    task_row = await session.get(Task, task_id)
                    if task_row and task_row.cancel_requested_at:
                        ctx.cancel_requested = True
                    ok = await heartbeat_lease(
                        session,
                        run_id=run_id,
                        worker_id=self.worker_id,
                        lease_ttl_seconds=self.settings.lease_ttl_seconds,
                    )
                    await session.commit()
                    if not ok:
                        ctx.cancel_requested = True
                        return

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            async with asyncio.timeout(self.settings.max_run_duration_seconds):
                result = await agent.run(
                    ctx,
                    llm=self.llm,
                    sandbox=provider,
                    sandbox_ref=sandbox_ref,
                    mcp_sessions=mcp_sessions,
                    on_step=on_step,
                    on_boundary=on_boundary,
                )
        except Exception as exc:
            logger.exception("Agent run failed")
            async with factory() as session:
                await save_checkpoint(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    blob={"error": str(exc), "last_message_seq": ctx.last_message_seq},
                    metadata={"fatal": True},
                )
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=False,
                    error_code="agent_exception",
                    error_message=str(exc),
                )
                await session.commit()
            await slack_reply.finish(
                "The agent encountered an error. The task details contain the failure information.",
                status="failed",
            )
            return
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        repo_outcome = (
            await _collect_repo_outcome(provider, sandbox_ref, repo=ctx.repo)
            if result.success and ctx.repo
            else {}
        )

        async with factory() as session:
            if result.checkpoint_blob:
                await save_checkpoint(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    blob={**result.checkpoint_blob, "last_message_seq": ctx.last_message_seq},
                )
            task_result = await session.execute(
                select(Task).where(Task.task_id == task_id).with_for_update()
            )
            task_row = task_result.scalar_one()
            if repo_outcome:
                task_row.metadata_ = {**(task_row.metadata_ or {}), **repo_outcome}
            pending_result = await session.execute(
                select(Message.message_id)
                .where(Message.task_id == task_id, Message.seq > ctx.last_message_seq)
                .limit(1)
            )
            has_trailing_message = pending_result.scalar_one_or_none() is not None
            if result.park:
                await park_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    reason=result.park_reason or ParkReason.PLAN_APPROVAL.value,
                    approval_payload=result.approval_payload,
                )
                reply_status = task_row.park_reason or "agent_question"
            else:
                await complete_task(
                    session,
                    task_id=task_id,
                    run_id=run_id,
                    success=result.success,
                    error_code=result.error_code,
                    error_message=None if result.success else result.final_message,
                )
                reply_status = task_row.status
                if has_trailing_message and not task_row.cancel_requested_at:
                    task_row.status = TaskStatus.QUEUED.value
                    task_row.park_reason = None
                    task_row.updated_at = datetime.now(UTC)
                    await append_event(
                        session,
                        task_id=task_id,
                        run_id=run_id,
                        event_type="trailing_message_requeued",
                        payload={"last_message_seq": ctx.last_message_seq},
                    )
                    await enqueue_task_work(
                        session,
                        org_id=task_row.org_id,
                        task_id=task_id,
                        agent_type=task_row.agent_type,
                        reason="trailing_message",
                    )
            await session.commit()

        await slack_reply.finish(
            result.final_message,
            status=reply_status,
            pr_url=repo_outcome.get("pr_url") if isinstance(repo_outcome, dict) else None,
        )


def main() -> None:
    worker = HarnessWorker()

    loop = asyncio.new_event_loop()

    def _stop(*_args: Any) -> None:
        worker.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    loop.run_until_complete(worker.start())


if __name__ == "__main__":
    main()
