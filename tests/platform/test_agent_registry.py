import uuid
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from alephat_platform.common.config import clear_settings_cache
from alephat_platform.harness import agents
from alephat_platform.harness.agents import REGISTRY, RunContext, get_agent
from alephat_platform.harness.sandboxes import SandboxRef
from alephat_platform.harness.slack import SlackReply


def test_all_agent_types():
    for name in ("coding", "reviewer", "analyzer", "chat"):
        assert get_agent(name) is REGISTRY[name]


@pytest.mark.asyncio
async def test_agent_resumes_checkpoint_and_injects_boundary_guidance():
    seen_messages = []

    class FakeLlm:
        async def chat_completions(self, *, model, messages, **_kwargs):
            seen_messages.extend(messages)
            return {"choices": [{"message": {"content": "DONE: complete"}}]}

        def message_content(self, response):
            return response["choices"][0]["message"]["content"]

    class FakeSandbox:
        async def exec(self, *_args, **_kwargs):
            raise AssertionError("sandbox should not be called")

    events = []

    async def on_step(event_type, payload):
        events.append((event_type, payload))

    first = True

    async def on_boundary():
        nonlocal first
        if first:
            first = False
            return [{"role": "user", "content": "new guidance"}]
        return []

    context = RunContext(
        org_id="org",
        task_id="task",
        run_id="run",
        thread_id="thread",
        agent_type="coding",
        repo=None,
        base_ref="main",
        model="fake:model",
        prompt=None,
        messages=[],
        sandbox_provider="daytona",
        sandbox_id="sandbox",
        mcp_snapshot=[],
        checkpoint={"step": 2, "messages": [{"role": "user", "content": "old"}]},
    )
    result = await get_agent("coding").run(
        context,
        llm=FakeLlm(),
        sandbox=FakeSandbox(),
        sandbox_ref=SandboxRef(provider="daytona", sandbox_id="sandbox"),
        mcp_sessions=[],
        on_step=on_step,
        on_boundary=on_boundary,
    )
    assert result.success
    assert [message["content"] for message in seen_messages[-2:]] == ["old", "new guidance"]
    assert any(event_type == "checkpoint" for event_type, _payload in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("slack", [False, True])
async def test_real_backend_uses_deep_agent_runtime(monkeypatch, slack):
    class FakeModel(FakeListChatModel):
        def bind_tools(self, *_args, **_kwargs):
            return self

    monkeypatch.setattr(
        agents,
        "_make_deep_agent_model",
        lambda _model, **_kwargs: FakeModel(responses=["deep agent complete"]),
    )
    events = []

    async def on_step(event_type, payload):
        events.append((event_type, payload))

    async def on_boundary():
        return []

    class RecordingReply(SlackReply):
        def __init__(self):
            super().__init__(uuid.uuid4(), uuid.uuid4(), {"channel": "D1", "thread_ts": "1.2"})
            self.enabled = True
            self.previews = []

        async def instructions(self):
            return ""

        async def preview(self, text):
            self.previews.append(text)

    reply = RecordingReply() if slack else None

    context = RunContext(
        org_id="org",
        task_id="task",
        run_id="run",
        thread_id="thread",
        agent_type="coding",
        repo=None,
        base_ref="main",
        model="fake:model",
        prompt="finish",
        messages=[],
        sandbox_provider="daytona",
        sandbox_id="sandbox",
        mcp_snapshot=[],
        checkpoint=None,
        slack_reply=reply,
    )
    result = await get_agent("coding").run(
        context,
        llm=None,
        sandbox=None,
        sandbox_ref=SandboxRef(
            provider="daytona",
            sandbox_id="sandbox",
            handle=StateBackend(),
        ),
        mcp_sessions=[],
        on_step=on_step,
        on_boundary=on_boundary,
    )
    assert result.success
    assert result.final_message == "deep agent complete"
    assert result.checkpoint_blob["runtime"] == "deepagents"
    assert [event for event, _payload in events].count("model_started") == 1
    if reply:
        assert reply.previews[-1] == result.final_message


def test_runtime_instructions_include_repo_rules_and_plan_mode():
    context = RunContext(
        org_id="org",
        task_id="task",
        run_id="run",
        thread_id="thread",
        agent_type="coding",
        repo="acme/example",
        base_ref="main",
        model="fake:model",
        prompt="finish",
        messages=[],
        sandbox_provider="daytona",
        sandbox_id="sandbox",
        mcp_snapshot=[],
        checkpoint=None,
        metadata={
            "custom_instructions": "Always run the contract tests.",
            "plan_mode": True,
        },
    )

    instructions = agents._runtime_instructions(context)

    assert "Always run the contract tests." in instructions
    assert "request plan approval before making changes" in instructions


def test_model_effort_kwargs_match_provider_contracts():
    assert agents._model_effort_kwargs("google_genai", "xhigh") == {"thinking_level": "high"}
    assert agents._model_effort_kwargs("openai", "medium") == {"reasoning_effort": "medium"}


def test_litellm_google_alias_uses_openai_compatible_effort(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "litellm")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm:4000")
    clear_settings_cache()

    try:
        with patch("langchain_openai.ChatOpenAI") as chat_openai:
            agents._make_deep_agent_model("google:gemini-3.5-flash", effort="medium")
    finally:
        clear_settings_cache()

    kwargs = chat_openai.call_args.kwargs
    assert kwargs["reasoning_effort"] == "medium"
    assert "thinking_level" not in kwargs
