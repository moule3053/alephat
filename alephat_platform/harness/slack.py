"""One run's rich Slack reply, backed by the task's persisted message timestamps."""

import asyncio
import base64
import logging
import shlex
import time
import uuid
from pathlib import PurePosixPath

from langchain_core.tools import StructuredTool, ToolException
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import select

from alephat_platform.common.config import get_settings
from alephat_platform.common.db import session_scope
from alephat_platform.common.models import Task
from alephat_platform.common.slack import (
    SlackAction,
    SlackActionSigner,
    SlackClient,
    SlackMessage,
    SlackRendererClient,
)
from alephat_platform.harness.sandboxes import SandboxProviderBase, SandboxRef
from openswe.prompts import prompt

logger = logging.getLogger(__name__)


class SlackReply:
    def __init__(self, task_id: uuid.UUID, run_id: uuid.UUID, metadata: dict[str, object]) -> None:
        settings = get_settings()
        self.task_id, self.run_id = task_id, run_id
        self.channel = str(metadata.get("channel") or "")
        self.thread_ts = str(metadata.get("thread_ts") or "")
        self.message_ts = str(metadata.get("message_ts") or self.thread_ts)
        self.enabled = bool(settings.slack_bot_token and self.channel and self.thread_ts)
        self.client = SlackClient(settings.slack_bot_token)
        self.renderer = SlackRendererClient(
            settings.slack_renderer_url, settings.slack_renderer_token
        )
        self.signer = SlackActionSigner(settings.slack_signing_secret or settings.slack_bot_token)
        self.timestamps: list[str] = []
        self.lock = asyncio.Lock()
        self.last_update = 0.0
        self.spec: dict[str, JsonValue] | None = None
        self.fallback_text = ""
        self.stream_disabled = False

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            async with session_scope() as session:
                task = await session.get(Task, self.task_id)
                saved = (task.metadata_ or {}).get("slack_reply", {}) if task else {}
                if saved.get("run_id") == str(self.run_id):
                    self.timestamps = list(saved.get("timestamps", []))
            await self.client.react(self.channel, self.message_ts, "eyes")
            await self.publish(markdown="Working on it…", controls="running")
        except Exception:
            logger.warning("Could not start rich Slack reply", exc_info=True)

    async def instructions(self) -> str:
        if not self.enabled:
            return ""
        try:
            catalog = await self.renderer.catalog()
        except Exception:
            logger.warning(
                "Slack renderer catalog unavailable; using Markdown replies", exc_info=True
            )
            return ""
        return prompt("platform/slack", catalog=catalog)

    async def publish(
        self,
        *,
        markdown: str | None = None,
        spec: dict[str, JsonValue] | None = None,
        controls: str = "",
        partial: bool = False,
    ) -> None:
        async with self.lock:
            rendered = await self.renderer.render(markdown=markdown, spec=spec, partial=partial)
            messages = rendered.messages
            if not messages:
                return
            if controls:
                controls_blocks = await self.controls(controls)
                if len(messages[-1].blocks) + len(controls_blocks) > 50:
                    messages.append(SlackMessage(text="Task status", blocks=controls_blocks))
                else:
                    messages[-1].blocks.extend(controls_blocks)
            for message in messages:
                await self.bind_actions(message)
            for index, message in enumerate(messages):
                if index < len(self.timestamps):
                    await self.client.update(self.channel, self.timestamps[index], message)
                else:
                    self.timestamps.append(
                        await self.client.post(self.channel, self.thread_ts, message)
                    )
                    await self.save()
            # Streaming can temporarily have more pages than the completed response.
            for timestamp in self.timestamps[len(messages) :]:
                await self.client.update(
                    self.channel, timestamp, SlackMessage(text="Continued in the response above.")
                )
            self.last_update = time.monotonic()

    async def save(self) -> None:
        async with session_scope() as session:
            task = (
                await session.execute(
                    select(Task).where(Task.task_id == self.task_id).with_for_update()
                )
            ).scalar_one()
            task.metadata_ = {
                **(task.metadata_ or {}),
                "slack_reply": {
                    "run_id": str(self.run_id),
                    "timestamps": self.timestamps,
                },
            }

    async def bind_actions(self, message: SlackMessage) -> None:
        for block in message.blocks:
            if block.get("type") != "actions":
                continue
            elements = block.get("elements")
            if not isinstance(elements, list):
                continue
            for button in elements:
                if (
                    not isinstance(button, dict)
                    or button.get("url")
                    or button.get("type") != "button"
                ):
                    continue
                text = button.get("text")
                label = str(text.get("text") or "Action") if isinstance(text, dict) else "Action"
                action = str(button.get("action_id") or "reply")
                button["action_id"] = f"alephat:{action}"
                button["value"] = await self.signer.sign(
                    SlackAction(
                        task_id=self.task_id,
                        run_id=self.run_id,
                        channel=self.channel,
                        thread_ts=self.thread_ts,
                        action=action,
                        label=label,
                        value=str(button.get("value") or ""),
                        expires_at=int(time.time()) + 86400,
                    )
                )

    async def controls(self, status: str) -> list[dict[str, JsonValue]]:
        elements: list[JsonValue] = []
        if status == "running":
            elements.append(
                {
                    "type": "button",
                    "action_id": "cancel",
                    "text": {"type": "plain_text", "text": "Cancel task"},
                    "style": "danger",
                }
            )
        elif status == "plan_approval":
            elements.extend(
                [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "text": {"type": "plain_text", "text": "Approve plan"},
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "action_id": "reject",
                        "text": {"type": "plain_text", "text": "Request revision"},
                    },
                ]
            )
        blocks: list[dict[str, JsonValue]] = []
        if elements:
            blocks.append({"type": "actions", "elements": elements})
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": {
                            "running": "Working…",
                            "plan_approval": "Waiting for plan approval.",
                            "agent_question": "Reply in the DM, or mention the bot in this channel thread, to continue.",
                            "succeeded": "✓ Completed",
                            "failed": "Task failed. See details above.",
                            "cancelled": "Task cancelled.",
                        }.get(status, "Waiting for your response."),
                    }
                ],
            }
        )
        return blocks

    async def preview(self, text: str) -> None:
        if (
            not self.enabled
            or self.stream_disabled
            or self.spec is not None
            or time.monotonic() - self.last_update < 1.5
        ):
            return
        try:
            await self.publish(markdown=text, controls="running")
        except Exception:
            self.stream_disabled = True
            logger.warning(
                "Slack streaming stopped; final reply will still be attempted", exc_info=True
            )

    async def finish(self, text: str, *, status: str, pr_url: str | None = None) -> None:
        if not self.enabled:
            return
        if pr_url:
            text += f"\n\n[Open pull request]({pr_url})"
        try:
            await self.publish(
                markdown=text if self.spec is None or status != "succeeded" else None,
                spec=self.spec if status == "succeeded" else None,
                controls=status,
            )
            if pr_url and self.spec is not None:
                await self.client.post(
                    self.channel, self.thread_ts, SlackMessage(text=f"Pull request: {pr_url}")
                )
        except Exception:
            logger.warning("Rich Slack reply failed; sending a plain-text fallback", exc_info=True)
            try:
                fallback = self.fallback_text or text or "Task completed."
                for index in range(0, len(fallback), 35000):
                    message = (
                        SlackMessage(
                            text=fallback[index : index + 35000],
                            blocks=[
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "plain_text",
                                        "text": fallback[index : index + 2900],
                                    },
                                }
                            ],
                        )
                        if len(fallback) <= 2900
                        else SlackMessage(text=fallback[index : index + 35000])
                    )
                    await self.client.post(self.channel, self.thread_ts, message)
            except Exception:
                logger.exception("Slack fallback delivery failed")
        try:
            if status in {"succeeded", "failed", "cancelled"}:
                await self.client.react(
                    self.channel,
                    self.message_ts,
                    "white_check_mark" if status == "succeeded" else "x",
                )
        except Exception:
            logger.warning("Slack completion reaction failed", exc_info=True)


class SlackTools:
    def __init__(self, reply: SlackReply, sandbox: SandboxProviderBase, ref: SandboxRef) -> None:
        self.reply, self.sandbox, self.ref = reply, sandbox, ref

    async def tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                coroutine=self.render_card,
                name="render_slack_card",
                description=prompt("tools/platform_slack_card"),
                handle_tool_error=True,
            ),
            StructuredTool.from_function(
                coroutine=self.upload_file,
                name="upload_slack_file",
                description=prompt("tools/platform_slack_file"),
                handle_tool_error=True,
            ),
        ]

    async def render_card(self, spec_json: str, fallback_text: str) -> dict[str, JsonValue]:
        try:
            spec = TypeAdapter(dict[str, JsonValue]).validate_json(spec_json)
            await self.reply.publish(spec=spec)
        except Exception as exc:
            logger.warning("Slack card could not be rendered", exc_info=True)
            raise ToolException(str(exc)) from exc
        self.reply.spec, self.reply.fallback_text = spec, fallback_text
        return {"posted": True, "message_count": len(self.reply.timestamps)}

    async def upload_file(self, path: str, title: str) -> dict[str, JsonValue]:
        script = (
            "import base64,pathlib,sys\n"
            "root=pathlib.Path.cwd().resolve()\n"
            "p=pathlib.Path(sys.argv[1]).resolve()\n"
            "if not p.is_relative_to(root):\n"
            "    raise ValueError('File must be inside the sandbox workspace')\n"
            "if not p.is_file() or p.stat().st_size > 10*1024*1024:\n"
            "    raise ValueError('Maximum file size is 10 MiB')\n"
            "print(base64.b64encode(p.read_bytes()).decode())"
        )
        try:
            result = await self.sandbox.exec(
                self.ref, f"python -c {shlex.quote(script)} {shlex.quote(path)}", timeout=30
            )
            if result.exit_code:
                raise ValueError(result.stderr[-1000:] or "Cannot read sandbox artifact")
            content = base64.b64decode(result.stdout.strip(), validate=True)
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("Maximum file size is 10 MiB")
            file_id = await self.reply.client.upload(
                self.reply.channel,
                self.reply.thread_ts,
                filename=PurePosixPath(path).name,
                content=content,
                title=title,
            )
        except Exception as exc:
            logger.warning("Slack artifact upload failed", exc_info=True)
            raise ToolException(str(exc)) from exc
        return {"file_id": file_id, "uploaded": True}
