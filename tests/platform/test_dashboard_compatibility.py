"""Tests for the dashboard compatibility layer."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alephat_platform.api.deps import AuthContext
from alephat_platform.api.routes_dashboard import (
    AgentInstructionsCreate,
    AgentInstructionsUpdate,
    DashboardMessageRequest,
    DashboardProfileUpdate,
    TeamSettingsUpdate,
    _get_langgraph_messages_for_task,
    _get_messages_for_task,
    _normalize_langgraph_message,
    _protocol_event,
    _sse_data,
    _terminal_lifecycle,
    create_agent_instructions,
    get_agent_usage_leaderboard,
    get_me,
    get_options,
    get_profile,
    get_repos,
    get_sidebar_threads,
    get_team_settings,
    list_agent_instructions,
    post_thread_commands,
    post_thread_message,
    put_agent_instructions,
    put_profile,
    put_team_settings,
    task_to_agent_thread,
)
from alephat_platform.common.config import get_settings
from alephat_platform.common.models import Message, OrgSettings, Task, UserSettings


@pytest.mark.asyncio
async def test_get_messages_includes_uncheckpointed_db_messages(patch_load_checkpoint=None):
    db = AsyncMock()
    task_id = uuid.uuid4()

    mock_cp = MagicMock()
    mock_cp.blob = {
        "last_message_seq": 1,
        "messages": [{"type": "human", "data": {"id": "msg-0", "content": "hello"}}],
    }

    mock_msg = Message(
        message_id=uuid.uuid4(),
        task_id=task_id,
        seq=2,
        role="user",
        content="who is the US president now",
        created_at=datetime.now(UTC),
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_msg]
    db.execute.return_value = mock_result

    with patch(
        "alephat_platform.api.routes_dashboard.load_latest_checkpoint", return_value=mock_cp
    ):
        lg_messages = await _get_langgraph_messages_for_task(db, task_id)
        ui_messages = await _get_messages_for_task(db, task_id)

    assert len(lg_messages) == 2
    assert lg_messages[0]["content"] == "hello"
    assert lg_messages[1]["content"] == "who is the US president now"

    assert len(ui_messages) == 2
    assert ui_messages[0]["chunks"][0]["text"] == "hello"
    assert ui_messages[1]["chunks"][0]["text"] == "who is the US president now"


@pytest.mark.asyncio
async def test_get_messages_flattens_structured_text_content():
    db = AsyncMock()
    task_id = uuid.uuid4()
    mock_cp = MagicMock()
    mock_cp.blob = {
        "last_message_seq": 0,
        "messages": [
            {
                "type": "ai",
                "data": {
                    "id": "msg-tool-call",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tool-1",
                            "name": "read_file",
                            "args": {"path": "README.md"},
                        }
                    ],
                },
            },
            {
                "type": "tool",
                "data": {
                    "id": "msg-tool-result",
                    "content": "the entire raw README should stay compacted",
                    "tool_call_id": "tool-1",
                },
            },
            {
                "type": "ai",
                "data": {
                    "id": "msg-0",
                    "content": [
                        {
                            "type": "text",
                            "text": "structured response",
                            "extras": {"signature": "provider-metadata"},
                        }
                    ],
                },
            },
        ],
    }
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_result

    with patch(
        "alephat_platform.api.routes_dashboard.load_latest_checkpoint", return_value=mock_cp
    ):
        messages = await _get_messages_for_task(db, task_id)

    assert len(messages) == 1
    assert messages[0]["id"] == "msg-tool-call"
    assert messages[0]["author"] == {"name": "agent"}
    assert messages[0]["startedAt"]
    assert messages[0]["chunks"] == [
        {
            "kind": "tool-execution",
            "toolCallId": "tool-1",
            "title": "read_file",
            "toolKind": "read",
            "input": {"path": "README.md"},
            "status": "completed",
        },
        {"kind": "text", "text": "structured response"},
    ]


def test_stream_values_use_agent_protocol_event_envelope():
    event = _protocol_event("values", {"messages": [{"type": "ai", "content": "done"}]})

    assert event["type"] == "event"
    assert event["method"] == "values"
    assert event["params"]["namespace"] == []
    assert event["params"]["data"]["messages"][0]["content"] == "done"
    assert _sse_data(event).startswith('data: {"type":"event","method":"values"')


@pytest.mark.parametrize(
    ("status", "event"),
    [
        ("succeeded", "completed"),
        ("failed", "failed"),
        ("cancelled", "interrupted"),
        ("parked", "interrupted"),
        ("running", None),
    ],
)
def test_terminal_lifecycle(status: str, event: str | None):
    lifecycle = _terminal_lifecycle(status)
    assert (lifecycle or {}).get("event") == event


@pytest.mark.asyncio
async def test_commands_return_agent_protocol_success():
    task = Task(
        task_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        source="web_ui",
        title="Existing task",
        repo="acme/test-repo",
        base_ref="main",
        model="gpt-4o",
        sandbox_provider="local",
        status="queued",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = task
    db = AsyncMock()
    db.execute.return_value = result
    db.get.return_value = None
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "id": 7,
            "method": "run.start",
            "params": {"assistant_id": "agent", "input": {}},
        }
    )

    response = await post_thread_commands(
        task.thread_id,
        request,
        db,
        AuthContext(org_id=task.org_id, user_id=uuid.uuid4()),
    )

    assert response == {
        "type": "success",
        "id": 7,
        "result": {"run_id": str(task.task_id)},
    }


@pytest.mark.asyncio
async def test_commands_preserve_the_optimistic_message_id():
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    thread_id = str(uuid.uuid4())
    message_id = uuid.uuid4()
    task = Task(
        task_id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread_id,
        source="web_ui",
        title="New task",
        model="google:gemini-3.5-flash",
        sandbox_provider="local",
        status="queued",
    )
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "id": 8,
            "method": "run.start",
            "params": {
                "assistant_id": "agent",
                "config": {
                    "configurable": {
                        "agent_model_id": "anthropic:claude-sonnet-5",
                        "agent_effort": "high",
                        "repo": "acme/test-repo",
                        "plan_mode": True,
                    }
                },
                "input": {
                    "messages": [
                        {
                            "type": "human",
                            "id": str(message_id),
                            "content": "Reply once",
                        }
                    ]
                },
            },
        }
    )
    db = AsyncMock()
    db.get.side_effect = [
        None,
        OrgSettings(
            org_id=org_id,
            default_model="google:gemini-3.5-flash",
            default_sandbox_provider="local",
            settings={
                "agent_instructions": {
                    "acme/test-repo": {"instructions": "Run the repository contract tests."}
                }
            },
        ),
    ]

    with (
        patch(
            "alephat_platform.api.routes_dashboard._get_task_by_id_or_thread_id",
            return_value=None,
        ),
        patch(
            "alephat_platform.api.routes_tasks.create_task",
            new=AsyncMock(return_value=task),
        ) as create_task,
    ):
        response = await post_thread_commands(
            thread_id,
            request,
            db,
            AuthContext(org_id=org_id, user_id=user_id),
        )

    assert create_task.await_args.kwargs["prompt_message_id"] == message_id
    assert create_task.await_args.kwargs["model"] == "anthropic:claude-sonnet-5"
    assert create_task.await_args.kwargs["repo"] == "acme/test-repo"
    assert create_task.await_args.kwargs["metadata"] == {
        "agent_effort": "high",
        "plan_mode": True,
        "custom_instructions": "Run the repository contract tests.",
    }
    assert response["type"] == "success"


def test_message_id_survives_worker_context_and_checkpoint():
    from alephat_platform.harness import agents
    from alephat_platform.harness.worker import _message_input

    message_id = uuid.uuid4()
    db_message = Message(
        message_id=message_id,
        task_id=uuid.uuid4(),
        role="user",
        kind="user_input",
        content="Reply once",
    )

    context_message = _message_input(db_message)
    langchain_message = agents._context_message(context_message)
    checkpoint_message = agents._checkpoint_messages([langchain_message])[0]
    streamed_message = _normalize_langgraph_message(checkpoint_message, 0)

    assert context_message["id"] == str(message_id)
    assert langchain_message.id == str(message_id)
    assert streamed_message["id"] == str(message_id)


@pytest.mark.asyncio
async def test_get_me():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), role="admin")
    res = await get_me(auth)
    assert res["login"] == "moule3053"
    assert res["is_admin"] is True


@pytest.mark.asyncio
async def test_get_options():
    res = await get_options()
    assert len(res["models"]) > 0
    assert res["default_agent_model"] == get_settings().default_model
    gemini = next(model for model in res["models"] if model["id"] == "google:gemini-3.5-flash")
    assert gemini["context_window"] == 1_048_576


@pytest.mark.asyncio
async def test_get_profile():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    db = AsyncMock()
    db.get.return_value = None

    res = await get_profile(db, auth)

    assert res["login"] == "moule3053"
    assert res["default_model"] == get_settings().default_model
    assert res["create_prs"] is False


@pytest.mark.asyncio
async def test_put_profile_persists_dashboard_fields():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    row = UserSettings(user_id=auth.user_id, settings={"branch_prefix": "old/"})
    db = AsyncMock()
    db.get.return_value = row
    body = DashboardProfileUpdate(
        default_model="anthropic:claude-sonnet-5",
        reasoning_effort="high",
        default_subagent_model="google:gemini-3.5-flash",
        subagent_reasoning_effort="medium",
        default_repo="acme/test-repo",
        base_branch="develop",
        branch_prefix="alephat/",
        auto_fix_ci=False,
        create_prs=True,
        review_draft_prs=True,
    )

    res = await put_profile(body, db, auth)

    assert row.preferred_model == "anthropic:claude-sonnet-5"
    assert row.settings["default_repo"] == "acme/test-repo"
    assert row.settings["base_branch"] == "develop"
    assert res["create_prs"] is True
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_repos_matches_dashboard_contract():
    with patch(
        "alephat_platform.api.routes_dashboard._fetch_installed_github_repositories",
        return_value=(
            [{"id": 42, "account": "acme", "account_type": "Organization"}],
            [
                {"full_name": "acme/private-repo", "private": True},
                {"full_name": "acme/public-repo", "private": False},
            ],
        ),
    ):
        res = await get_repos()

    assert res["installations"][0]["id"] == 42
    assert res["repositories"] == [
        {"full_name": "acme/private-repo", "private": True},
        {"full_name": "acme/public-repo", "private": False},
    ]


@pytest.mark.asyncio
async def test_team_settings_persist_in_org_settings():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    org = OrgSettings(
        org_id=auth.org_id,
        default_model="openai:gpt-4o-mini",
        default_sandbox_provider="local",
        settings={},
    )
    db = AsyncMock()
    db.get.return_value = org
    body = TeamSettingsUpdate(
        default_agent_model="anthropic:claude-sonnet-5",
        default_agent_reasoning_effort="high",
        org_guidelines="Keep changes small.",
    )

    saved = await put_team_settings(body, db, auth)
    loaded = await get_team_settings(db, auth)

    assert org.default_model == "anthropic:claude-sonnet-5"
    assert saved["org_guidelines"] == "Keep changes small."
    assert loaded["default_agent_reasoning_effort"] == "high"
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_instructions_are_persisted_by_repo():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    org = OrgSettings(
        org_id=auth.org_id,
        default_model="openai:gpt-4o-mini",
        default_sandbox_provider="local",
        settings={},
    )
    db = AsyncMock()
    db.get.return_value = org

    created = await create_agent_instructions(
        AgentInstructionsCreate(full_name="acme/example"), db, auth
    )
    saved = await put_agent_instructions(
        "acme/example",
        AgentInstructionsUpdate(instructions="Always run contract tests."),
        db,
        auth,
    )
    records = await list_agent_instructions(db, auth)

    assert created["owner"] == "acme"
    assert saved["instructions"] == "Always run contract tests."
    assert records == [saved]


@pytest.mark.asyncio
async def test_usage_leaderboard_counts_platform_tasks():
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    task = Task(
        task_id=uuid.uuid4(),
        org_id=auth.org_id,
        created_by=auth.user_id,
        thread_id=str(uuid.uuid4()),
        source="web_ui",
        agent_type="coding",
        model="google:gemini-3.5-flash",
        sandbox_provider="local",
        status="succeeded",
        metadata_={"pr_url": "https://example.test/pr/1", "additions": 7, "deletions": 2},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [task]
    db = AsyncMock()
    db.execute.return_value = result

    payload = await get_agent_usage_leaderboard("30d", 10, db, auth)

    assert payload["rows"][0]["agent_runs"] == 1
    assert payload["rows"][0]["agent_loc"] == 9
    assert payload["rows"][0]["prs_opened"] == 1


def test_task_to_agent_thread():
    task = Task(
        task_id=uuid.uuid4(),
        title="Test Task",
        repo="acme/test-repo",
        base_ref="main",
        model="gpt-4o",
        status="queued",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    thread = task_to_agent_thread(task)
    assert thread["id"] == str(task.task_id)
    assert thread["title"] == "Test Task"
    assert thread["repo"] == "test-repo"
    assert thread["repoFullName"] == "acme/test-repo"
    assert thread["status"] == "running"
    assert thread["resolved"] is False


def test_finished_thread_is_unread_until_current_run_is_viewed():
    run_id = uuid.uuid4()
    task = Task(
        task_id=uuid.uuid4(),
        active_run_id=run_id,
        title="Finished task",
        model="google:gemini-3.5-flash",
        status="succeeded",
        metadata_={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    unread = task_to_agent_thread(task)
    task.metadata_ = {"last_viewed_run_id": str(run_id), "last_viewed_at_ms": 123}
    viewed = task_to_agent_thread(task)

    assert unread["status"] == "finished"
    assert unread["viewed"] is False
    assert viewed["viewed"] is True


def test_normalized_ai_message_preserves_usage_metadata():
    message = _normalize_langgraph_message(
        {
            "type": "ai",
            "data": {
                "id": "answer-1",
                "content": "done",
                "usage_metadata": {
                    "input_tokens": 1200,
                    "output_tokens": 50,
                    "total_tokens": 1250,
                },
            },
        },
        0,
    )

    assert message["usage_metadata"]["total_tokens"] == 1250


@pytest.mark.asyncio
async def test_message_arriving_after_terminal_transition_starts_follow_up_run():
    message_id = uuid.uuid4()
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())
    task = Task(
        task_id=uuid.uuid4(),
        org_id=auth.org_id,
        thread_id=str(uuid.uuid4()),
        source="web_ui",
        agent_type="coding",
        model="google:gemini-3.5-flash",
        sandbox_provider="local",
        status="succeeded",
        active_run_id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db = AsyncMock()

    with (
        patch(
            "alephat_platform.api.routes_dashboard._get_task_by_id_or_thread_id",
            return_value=task,
        ),
        patch("alephat_platform.api.routes_dashboard.add_message", new=AsyncMock()) as add_message,
        patch(
            "alephat_platform.api.routes_dashboard.enqueue_task_work", new=AsyncMock()
        ) as enqueue,
        patch(
            "alephat_platform.api.routes_dashboard._get_messages_for_task",
            return_value=[],
        ),
    ):
        response = await post_thread_message(
            task.thread_id,
            DashboardMessageRequest(content="One more question", client_message_id=str(message_id)),
            db,
            auth,
        )

    assert task.status == "queued"
    add_message.assert_awaited_once()
    assert add_message.await_args.kwargs["message_id"] == message_id
    assert enqueue.await_args.kwargs["reason"] == "follow_up_race"
    assert response["status"] == "running"


@pytest.mark.asyncio
async def test_get_sidebar_threads():
    db = AsyncMock()
    auth = AuthContext(org_id=uuid.uuid4(), user_id=uuid.uuid4())

    task1 = Task(
        task_id=uuid.uuid4(),
        title="Active Task",
        repo="acme/test-repo",
        base_ref="main",
        model="gpt-4o",
        status="queued",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    task2 = Task(
        task_id=uuid.uuid4(),
        title="Resolved Task",
        repo="acme/test-repo",
        base_ref="main",
        model="gpt-4o",
        status="succeeded",
        metadata_={"resolved": True},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [task1, task2]
    db.execute.return_value = mock_result

    res = await get_sidebar_threads(db, auth)
    assert "active" in res
    assert "resolved" in res
    assert len(res["active"]["items"]) == 1
    assert len(res["resolved"]["items"]) == 1
    assert res["active"]["items"][0]["title"] == "Active Task"
    assert res["resolved"]["items"][0]["title"] == "Resolved Task"
