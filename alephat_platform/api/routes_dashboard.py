"""FastAPI dashboard compatibility router."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alephat_platform.api.deps import AuthContext, get_auth, get_db
from alephat_platform.api.routes_tasks import add_message, cancel_task, decide_approval
from alephat_platform.common.config import get_settings
from alephat_platform.common.enums import ApprovalStatus, TaskStatus
from alephat_platform.common.models import Approval, Message, OrgSettings, Task, UserSettings
from alephat_platform.common.outbox import enqueue_task_work
from alephat_platform.common.state_machine import is_terminal
from alephat_platform.common.tasks import load_latest_checkpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard/api", tags=["dashboard"])


def _protocol_success(command_id: int, run_id: str) -> dict[str, Any]:
    return {
        "type": "success",
        "id": command_id,
        "result": {"run_id": run_id},
    }


def _protocol_event(method: str, data: Any) -> dict[str, Any]:
    return {
        "type": "event",
        "method": method,
        "params": {
            "namespace": [],
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data,
        },
    }


def _sse_data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _terminal_lifecycle(status: str) -> dict[str, Any] | None:
    if status == TaskStatus.SUCCEEDED.value:
        return {"event": "completed"}
    if status == TaskStatus.FAILED.value:
        return {"event": "failed", "error": "Agent run failed"}
    if status in {TaskStatus.PARKED.value, TaskStatus.CANCELLED.value}:
        return {"event": "interrupted"}
    return None


def _submitted_message_id(message: Any) -> uuid.UUID | None:
    if not isinstance(message, dict):
        return None
    try:
        return uuid.UUID(str(message.get("id")))
    except TypeError, ValueError, AttributeError:
        return None


def _command_configurable(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    config = params.get("config")
    if not isinstance(config, dict):
        return {}
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, dict) else {}


async def _fetch_installed_github_repositories() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    from openswe.github.app import (
        GITHUB_APP_INSTALLATION_ID,
        get_github_app_installation_token,
    )

    token = await get_github_app_installation_token()
    if not token:
        raise HTTPException(status_code=503, detail="GitHub App credentials are not configured")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repositories: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        page = 1
        while len(repositories) < 1000:
            try:
                response = await client.get(
                    "https://api.github.com/installation/repositories",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise HTTPException(
                        status_code=401,
                        detail="GitHub App token expired or was revoked",
                    ) from exc
                raise HTTPException(
                    status_code=502,
                    detail=f"GitHub API error ({exc.response.status_code})",
                ) from exc
            except httpx.RequestError as exc:
                raise HTTPException(status_code=503, detail="GitHub API request failed") from exc

            body = response.json()
            page_repositories = body.get("repositories", []) if isinstance(body, dict) else []
            if not isinstance(page_repositories, list):
                break
            repositories.extend(item for item in page_repositories if isinstance(item, dict))
            if len(page_repositories) < 100:
                break
            page += 1

    installation_id = int(GITHUB_APP_INSTALLATION_ID) if GITHUB_APP_INSTALLATION_ID else 0
    installations = (
        [{"id": installation_id, "account": None, "account_type": None}] if installation_id else []
    )
    return installations, repositories


async def _get_task_by_id_or_thread_id(
    db: AsyncSession,
    org_id: uuid.UUID,
    thread_id: str,
    *,
    for_update: bool = False,
) -> Task | None:
    statement = select(Task).where(Task.org_id == org_id, Task.thread_id == thread_id)
    result = await db.execute(statement.with_for_update() if for_update else statement)
    task = result.scalars().first()
    if task:
        return task

    try:
        task_id = uuid.UUID(thread_id)
        if for_update:
            result = await db.execute(
                select(Task).where(Task.org_id == org_id, Task.task_id == task_id).with_for_update()
            )
            return result.scalars().first()
        task = await db.get(Task, task_id)
        if task and task.org_id == org_id:
            return task
    except ValueError:
        pass

    return None


class DashboardMessageRequest(BaseModel):
    content: str
    client_message_id: uuid.UUID | None = None


class DashboardProfileUpdate(BaseModel):
    default_model: str
    reasoning_effort: str
    default_subagent_model: str | None = None
    subagent_reasoning_effort: str | None = None
    default_repo: str | None = None
    base_branch: str | None = None
    branch_prefix: str | None = None
    auto_fix_ci: bool = True
    create_prs: bool = False
    review_draft_prs: bool | None = None


class AgentInstructionsCreate(BaseModel):
    full_name: str


class AgentInstructionsUpdate(BaseModel):
    instructions: str = ""


class TeamSettingsUpdate(BaseModel):
    review_draft_prs: bool = False
    pr_summaries: bool = True
    review_trace_links: bool = True
    gateway_enabled: bool | None = None
    fable_enabled: bool = False
    review_tracing_project: str | None = None
    org_guidelines: str | None = None
    default_agent_model: str | None = None
    default_agent_reasoning_effort: str | None = None
    default_agent_subagent_model: str | None = None
    default_agent_subagent_reasoning_effort: str | None = None
    default_repo: str | None = None
    default_reviewer_model: str | None = None
    default_reviewer_reasoning_effort: str | None = None
    default_reviewer_subagent_model: str | None = None
    default_reviewer_subagent_reasoning_effort: str | None = None
    default_grouping_model: str | None = None
    default_grouping_reasoning_effort: str | None = None
    default_chat_model: str | None = None
    default_chat_reasoning_effort: str | None = None


def _normalize_repo_full_name(value: str) -> str:
    parts = value.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise HTTPException(status_code=422, detail="repository must use owner/name form")
    return f"{parts[0]}/{parts[1]}"


def _instruction_record(full_name: str, *, created_by: str) -> dict[str, Any]:
    owner, name = full_name.split("/", 1)
    now = datetime.now(UTC).isoformat()
    return {
        "full_name": full_name,
        "owner": owner,
        "name": name,
        "instructions": "",
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    }


def _agent_instruction_records(org: OrgSettings | None) -> dict[str, dict[str, Any]]:
    values = (org.settings or {}).get("agent_instructions", {}) if org else {}
    if not isinstance(values, dict):
        return {}
    return {str(key): dict(value) for key, value in values.items() if isinstance(value, dict)}


def _team_settings_response(org: OrgSettings | None) -> dict[str, Any]:
    values = dict((org.settings or {}).get("team_settings", {})) if org else {}
    default_model = org.default_model if org and org.default_model else get_settings().default_model
    return {
        "review_draft_prs": values.get("review_draft_prs", False),
        "pr_summaries": values.get("pr_summaries", True),
        "review_trace_links": values.get("review_trace_links", True),
        "gateway_enabled": values.get("gateway_enabled"),
        "fable_enabled": values.get("fable_enabled", False),
        "review_tracing_project": values.get("review_tracing_project"),
        "org_guidelines": values.get("org_guidelines"),
        "default_agent_model": values.get("default_agent_model") or default_model,
        "default_agent_reasoning_effort": values.get("default_agent_reasoning_effort", "medium"),
        "default_agent_subagent_model": values.get("default_agent_subagent_model") or default_model,
        "default_agent_subagent_reasoning_effort": values.get(
            "default_agent_subagent_reasoning_effort", "medium"
        ),
        "default_repo": values.get("default_repo"),
        "default_reviewer_model": values.get("default_reviewer_model"),
        "default_reviewer_reasoning_effort": values.get("default_reviewer_reasoning_effort"),
        "default_reviewer_subagent_model": values.get("default_reviewer_subagent_model"),
        "default_reviewer_subagent_reasoning_effort": values.get(
            "default_reviewer_subagent_reasoning_effort"
        ),
        "default_grouping_model": values.get("default_grouping_model"),
        "default_grouping_reasoning_effort": values.get("default_grouping_reasoning_effort"),
        "default_chat_model": values.get("default_chat_model"),
        "default_chat_reasoning_effort": values.get("default_chat_reasoning_effort"),
        "updated_at": org.updated_at.isoformat() if org and org.updated_at else None,
    }


async def _get_or_create_org_settings(db: AsyncSession, org_id: uuid.UUID) -> OrgSettings:
    org = await db.get(OrgSettings, org_id)
    if org is None:
        settings = get_settings()
        org = OrgSettings(
            org_id=org_id,
            default_model=settings.default_model,
            default_sandbox_provider=settings.default_sandbox_provider,
        )
        db.add(org)
    return org


def _profile_response(
    row: UserSettings | None,
    *,
    default_model: str,
) -> dict[str, Any]:
    values = dict(row.settings or {}) if row else {}
    model = row.preferred_model if row and row.preferred_model else default_model
    return {
        "agent_workspace_tasks_supported": True,
        "login": "moule3053",
        "email": "14330171+moule3053@users.noreply.github.com",
        "default_model": model,
        "reasoning_effort": values.get("reasoning_effort", "medium"),
        "default_subagent_model": values.get("default_subagent_model") or model,
        "subagent_reasoning_effort": values.get("subagent_reasoning_effort")
        or values.get("reasoning_effort", "medium"),
        "default_repo": values.get("default_repo"),
        "base_branch": values.get("base_branch"),
        "branch_prefix": values.get("branch_prefix", "alephat/"),
        "auto_fix_ci": values.get("auto_fix_ci", True),
        "create_prs": values.get("create_prs", False),
        "review_draft_prs": values.get("review_draft_prs"),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.get("/me")
async def get_me(auth: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    login = auth.github_login or "moule3053"
    return {
        "login": login,
        "auth_provider": "github",
        "github_login": login,
        "display_name": login,
        "email": auth.email or "14330171+moule3053@users.noreply.github.com",
        "avatar_url": auth.avatar_url or "https://github.com/moule3053.png",
        "is_admin": auth.role == "admin",
        "slack_oauth_enabled": False,
    }


@router.get("/my-mapping")
async def get_my_mapping(auth: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    login = auth.github_login or "moule3053"
    return {"login": login, "github_login": login}


@router.get("/repos")
async def get_repos() -> dict[str, Any]:
    installations, repositories = await _fetch_installed_github_repositories()
    return {
        "installations": installations,
        "repositories": sorted(
            [
                {"full_name": str(item["full_name"]), "private": bool(item.get("private"))}
                for item in repositories
                if isinstance(item.get("full_name"), str) and item["full_name"]
            ],
            key=lambda item: str(item["full_name"]).casefold(),
        ),
    }


@router.get("/options")
async def get_options() -> dict[str, Any]:
    from openswe.dashboard.options import model_profile_context_window

    settings = get_settings()
    default_model = settings.default_model or "google:gemini-3.5-flash"
    models: list[dict[str, Any]] = [
        {
            "id": "google:gemini-4-argon",
            "label": "Gemini 4 Argon",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "supports_images": True,
        },
        {
            "id": "google_genai:gemini-4-argon",
            "label": "Gemini 4 Argon (GenAI)",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "supports_images": True,
        },
        {
            "id": "google:gemini-3.5-flash",
            "label": "Gemini 3.5 Flash",
            "efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
            "supports_images": True,
        },
        {
            "id": "google_genai:gemini-3.5-flash",
            "label": "Gemini 3.5 Flash (GenAI)",
            "efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
            "supports_images": True,
        },
        {
            "id": "openai:gpt-4o",
            "label": "GPT-4o",
            "efforts": [],
            "default_effort": "",
            "supports_images": True,
        },
        {
            "id": "openai:gpt-4o-mini",
            "label": "GPT-4o Mini",
            "efforts": [],
            "default_effort": "",
            "supports_images": True,
        },
        {
            "id": "anthropic:claude-3-5-sonnet",
            "label": "Claude 3.5 Sonnet",
            "efforts": [],
            "default_effort": "",
            "supports_images": True,
        },
        {
            "id": "anthropic:claude-sonnet-5",
            "label": "Claude Sonnet 5",
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "default_effort": "high",
            "supports_images": True,
        },
        {
            "id": "openai:o1",
            "label": "o1",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "supports_images": False,
        },
    ]
    for model in models:
        profile_id = (
            f"google_genai:{model['id'].split(':', 1)[1]}"
            if model["id"].startswith("google:")
            else model["id"]
        )
        context_window = model_profile_context_window(profile_id)
        if context_window is not None:
            model["context_window"] = context_window
    return {
        "models": models,
        "default_agent_model": default_model,
        "default_agent_reasoning_effort": "medium",
        "default_agent_subagent_model": default_model,
        "default_agent_subagent_reasoning_effort": "medium",
    }


@router.get("/profile")
async def get_profile(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> dict[str, Any]:
    row = await db.get(UserSettings, auth.user_id)
    org = await db.get(OrgSettings, auth.org_id)
    default_model = org.default_model if org and org.default_model else get_settings().default_model
    return _profile_response(row, default_model=default_model)


@router.put("/profile")
async def put_profile(
    body: DashboardProfileUpdate,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    row = await db.get(UserSettings, auth.user_id)
    if row is None:
        row = UserSettings(user_id=auth.user_id)
        db.add(row)

    row.preferred_model = body.default_model
    values = dict(row.settings or {})
    values.update(body.model_dump(exclude={"default_model"}))
    row.settings = values
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return _profile_response(row, default_model=body.default_model)


@router.get("/team-settings")
async def get_team_settings(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> dict[str, Any]:
    return _team_settings_response(await db.get(OrgSettings, auth.org_id))


@router.put("/team-settings")
async def put_team_settings(
    body: TeamSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    org = await _get_or_create_org_settings(db, auth.org_id)
    values = dict(org.settings or {})
    values["team_settings"] = body.model_dump()
    org.settings = values
    if body.default_agent_model:
        org.default_model = body.default_agent_model
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return _team_settings_response(org)


@router.get("/team-credentials")
async def get_team_credentials() -> dict[str, Any]:
    return {
        "datadog": {"connected": False, "updated_at": None},
        "langsmith": {"connected": False, "updated_at": None},
    }


@router.get("/my-credentials/currents")
async def get_my_currents_status() -> dict[str, Any]:
    return {"connected": False, "updated_at": None}


@router.get("/my-credentials/notion")
async def get_my_notion_status() -> dict[str, Any]:
    return {"connected": False, "updated_at": None}


@router.get("/admin/user-mappings")
async def get_admin_user_mappings(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    return {"items": [], "total": 0, "page": max(page, 1), "page_size": page_size}


@router.get("/agent-instructions")
async def list_agent_instructions(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> list[dict[str, Any]]:
    records = _agent_instruction_records(await db.get(OrgSettings, auth.org_id))
    return sorted(records.values(), key=lambda item: str(item.get("full_name", "")))


@router.post("/agent-instructions")
async def create_agent_instructions(
    body: AgentInstructionsCreate,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    full_name = _normalize_repo_full_name(body.full_name)
    org = await _get_or_create_org_settings(db, auth.org_id)
    records = _agent_instruction_records(org)
    if org is None or full_name not in records:
        records[full_name] = _instruction_record(full_name, created_by=str(auth.user_id))
        values = dict(org.settings or {})
        values["agent_instructions"] = records
        org.settings = values
        org.updated_at = datetime.now(UTC)
        await db.flush()
    return records[full_name]


@router.get("/agent-instructions/{full_name:path}")
async def get_agent_instructions(
    full_name: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    full_name = _normalize_repo_full_name(full_name)
    records = _agent_instruction_records(await db.get(OrgSettings, auth.org_id))
    record = records.get(full_name)
    if record is None:
        raise HTTPException(status_code=404, detail="agent instructions not found")
    return record


@router.put("/agent-instructions/{full_name:path}")
async def put_agent_instructions(
    full_name: str,
    body: AgentInstructionsUpdate,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    full_name = _normalize_repo_full_name(full_name)
    org = await _get_or_create_org_settings(db, auth.org_id)
    records = _agent_instruction_records(org)
    record = records.get(full_name) or _instruction_record(full_name, created_by=str(auth.user_id))
    record = {
        **record,
        "instructions": body.instructions,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    records[full_name] = record
    values = dict(org.settings or {})
    values["agent_instructions"] = records
    org.settings = values
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.delete("/agent-instructions/{full_name:path}", status_code=204)
async def delete_agent_instructions(
    full_name: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> Response:
    full_name = _normalize_repo_full_name(full_name)
    org = await db.get(OrgSettings, auth.org_id)
    records = _agent_instruction_records(org)
    if org is None or full_name not in records:
        raise HTTPException(status_code=404, detail="agent instructions not found")
    del records[full_name]
    values = dict(org.settings or {})
    values["agent_instructions"] = records
    org.settings = values
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return Response(status_code=204)


@router.get("/repo-snapshots")
async def list_repo_snapshots() -> list[dict[str, Any]]:
    return []


@router.get("/reviews")
async def list_reviews(
    page: int = 0,
    mine: bool = True,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del mine
    result = await db.execute(
        select(Task)
        .where(Task.org_id == auth.org_id, Task.agent_type == "reviewer")
        .order_by(Task.updated_at.desc())
        .offset(max(page, 0) * 20)
        .limit(21)
    )
    tasks = list(result.scalars().all())
    reviews = []
    for task in tasks[:20]:
        meta = task.metadata_ or {}
        full_name = str(meta.get("full_name") or task.repo or "")
        owner, _, repo = full_name.partition("/")
        pr_number = int(meta.get("pr_number") or 0)
        if not owner or not repo or not pr_number:
            continue
        reviews.append(
            {
                "thread_id": task.thread_id,
                "owner": owner,
                "repo": repo,
                "full_name": full_name,
                "number": pr_number,
                "title": task.title or f"PR #{pr_number}",
                "url": meta.get("pr_url") or f"https://github.com/{full_name}/pull/{pr_number}",
                "head_ref": meta.get("head_ref") or "",
                "base_ref": task.base_ref or "main",
                "author": meta.get("author") or "",
                "head_sha": meta.get("head_sha") or "",
                "watch": bool(meta.get("watch")),
                "status": "running" if task.status == TaskStatus.RUNNING.value else "idle",
                "counts": meta.get("counts")
                or {"open": 0, "resolved": 0, "dismissed": 0, "bugs": 0, "flags": 0},
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            }
        )
    return {"reviews": reviews, "page": max(page, 0), "has_more": len(tasks) > 20}


@router.get("/agent-usage-leaderboard")
async def get_agent_usage_leaderboard(
    period: str = "30d",
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    query = select(Task).where(Task.org_id == auth.org_id, Task.created_by == auth.user_id)
    if period in {"7d", "30d"}:
        query = query.where(Task.created_at >= datetime.now(UTC) - timedelta(days=int(period[:-1])))
    result = await db.execute(query)
    tasks = list(result.scalars().all())
    model_counts: dict[str, int] = {}
    additions = deletions = prs_opened = merged_prs = 0
    for task in tasks:
        model_counts[task.model] = model_counts.get(task.model, 0) + 1
        meta = task.metadata_ or {}
        additions += int(meta.get("additions") or 0)
        deletions += int(meta.get("deletions") or 0)
        prs_opened += int(bool(meta.get("pr_url")))
        merged_prs += int(bool(meta.get("pr_merged")))
    favorite_model = max(model_counts, key=model_counts.__getitem__) if model_counts else ""
    rows = []
    if tasks and limit > 0:
        rows.append(
            {
                "rank": 1,
                "user": {
                    "name": "moule3053",
                    "github_login": "moule3053",
                    "email": "14330171+moule3053@users.noreply.github.com",
                },
                "favorite_model": favorite_model,
                "agent_runs": len(tasks),
                "prs_opened": prs_opened,
                "merged_prs": merged_prs,
                "agent_loc": additions + deletions,
                "additions": additions,
                "deletions": deletions,
            }
        )
    reviewer_stats = {
        "period": period,
        "reviewed_prs": 0,
        "prs_with_findings": 0,
        "findings_recorded": 0,
        "surfaced_findings": 0,
        "addressed_findings": 0,
        "resolved_after_update": 0,
        "dismissed_findings": 0,
        "unresolved_surfaced_findings": 0,
        "resolution_rate": 0,
        "human_replies": 0,
        "severity_counts": {},
        "top_categories": [],
        "generated_at_ms": int(datetime.now(UTC).timestamp() * 1000),
    }
    return {
        "period": period,
        "rows": rows,
        "total_members": len(rows),
        "current_user_rank": 1 if rows else None,
        "generated_at_ms": int(datetime.now(UTC).timestamp() * 1000),
        "reviewer_stats": reviewer_stats,
    }


def _resolve_msg_type(raw_type: Any, role: Any = None) -> str:
    val = str(raw_type or role or "").lower()
    if "human" in val or "user" in val:
        return "human"
    if "ai" in val or "assistant" in val or "agent" in val:
        return "ai"
    if "tool" in val:
        return "tool"
    if "system" in val:
        return "system"
    return "human"


def _normalize_langgraph_message(m: Any, idx: int) -> dict[str, Any]:
    if not isinstance(m, dict):
        return {"type": "human", "id": f"cp-msg-{idx}", "content": str(m)}

    data = m.get("kwargs") or m.get("data") or m
    raw_type = (
        data.get("type")
        or m.get("type")
        or m.get("role")
        or (m.get("id")[-1] if isinstance(m.get("id"), list) and m.get("id") else None)
    )
    msg_type = _resolve_msg_type(raw_type)

    content = data.get("content") or m.get("content") or ""
    msg_id = data.get("id") or m.get("id")
    if not isinstance(msg_id, str):
        msg_id = f"cp-msg-{idx}"

    res: dict[str, Any] = {"type": msg_type, "id": msg_id, "content": content}
    if data.get("tool_calls"):
        res["tool_calls"] = data["tool_calls"]
    elif m.get("tool_calls"):
        res["tool_calls"] = m["tool_calls"]

    if data.get("invalid_tool_calls"):
        res["invalid_tool_calls"] = data["invalid_tool_calls"]
    if data.get("tool_call_id"):
        res["tool_call_id"] = data["tool_call_id"]
    if data.get("name"):
        res["name"] = data["name"]
    if data.get("artifact"):
        res["artifact"] = data["artifact"]
    if data.get("additional_kwargs"):
        res["additional_kwargs"] = data["additional_kwargs"]
    if data.get("response_metadata"):
        res["response_metadata"] = data["response_metadata"]
    if data.get("usage_metadata"):
        res["usage_metadata"] = data["usage_metadata"]

    return res


def _ui_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_ui_text_content(block) for block in content)
    if isinstance(content, dict):
        return _ui_text_content(content.get("text"))
    return ""


def _ui_tool_kind(name: str) -> str:
    lowered = name.lower()
    if lowered == "task":
        return "task"
    if lowered == "slack_thread_reply":
        return "slack"
    if lowered == "linear_comment":
        return "linear"
    if lowered in {"write_file", "edit_file", "str_replace", "write", "edit", "patch"}:
        return "edit"
    if lowered in {"execute", "bash", "shell", "run_terminal_cmd"}:
        return "execute"
    if lowered in {"fetch", "fetch_url", "http_request"}:
        return "fetch"
    if lowered in {"glob", "grep", "web_search", "search"}:
        return "search"
    if lowered in {"read_file", "read", "ls"} or "read" in lowered:
        return "read"
    if lowered == "think":
        return "think"
    return "other"


def _merge_ui_text_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text_indexes = [index for index, chunk in enumerate(chunks) if chunk.get("kind") == "text"]
    if len(text_indexes) <= 1:
        return chunks
    last_text_index = text_indexes[-1]
    return [
        chunk
        for index, chunk in enumerate(chunks)
        if chunk.get("kind") != "text" or index == last_text_index
    ]


async def _get_messages_for_task(session: AsyncSession, task_id: uuid.UUID) -> list[dict[str, Any]]:
    cp = await load_latest_checkpoint(session, task_id=task_id)
    checkpoint_last_seq = int((cp.blob if cp else {}).get("last_message_seq", 0))
    messages: list[dict[str, Any]] = []
    agent_turn: dict[str, Any] | None = None

    def flush_agent_turn() -> None:
        nonlocal agent_turn
        if agent_turn is not None:
            agent_turn["chunks"] = _merge_ui_text_chunks(agent_turn["chunks"])
            messages.append(agent_turn)
            agent_turn = None

    def append_agent_chunks(message_id: str, timestamp: str, chunks: list[dict[str, Any]]) -> None:
        nonlocal agent_turn
        if agent_turn is None:
            agent_turn = {
                "id": message_id,
                "author": {"name": "agent"},
                "timestamp": timestamp,
                "startedAt": timestamp,
                "chunks": list(chunks),
            }
            return
        agent_turn["timestamp"] = timestamp
        agent_turn["chunks"].extend(chunks)

    if cp and cp.blob and "messages" in cp.blob:
        for i, m in enumerate(cp.blob["messages"]):
            normalized = _normalize_langgraph_message(m, i)
            msg_type = normalized["type"]
            if msg_type in {"tool", "system"}:
                continue
            content = _ui_text_content(normalized.get("content"))
            chunks: list[dict[str, Any]] = []
            if content:
                chunks.append({"kind": "text", "text": content})
            if normalized.get("tool_calls"):
                for tool_index, tc in enumerate(normalized["tool_calls"]):
                    name = str(tc.get("name") or "tool")
                    if name in {"confirming_completion", "no_op"}:
                        continue
                    chunks.append(
                        {
                            "kind": "tool-execution",
                            "toolCallId": tc.get("id") or f"tc-{i}-{tool_index}",
                            "title": name,
                            "toolKind": _ui_tool_kind(name),
                            "input": tc.get("args") if isinstance(tc.get("args"), dict) else {},
                            "status": "completed",
                        }
                    )
            if not chunks:
                continue
            timestamp = datetime.now(UTC).isoformat()
            if msg_type == "human":
                flush_agent_turn()
                messages.append(
                    {
                        "id": normalized["id"],
                        "author": {"name": "user"},
                        "timestamp": timestamp,
                        "chunks": chunks,
                    }
                )
            elif msg_type == "ai":
                append_agent_chunks(normalized["id"], timestamp, chunks)

    msg_result = await session.execute(
        select(Message)
        .where(Message.task_id == task_id, Message.seq > checkpoint_last_seq)
        .order_by(Message.seq)
    )
    db_messages = list(msg_result.scalars().all())
    for m in db_messages:
        if m.role in {"user", "human", "user_guidance"}:
            flush_agent_turn()
            messages.append(
                {
                    "id": str(m.message_id),
                    "author": {"name": "user"},
                    "timestamp": m.created_at.isoformat(),
                    "chunks": [{"kind": "text", "text": m.content}],
                }
            )
        elif m.role in {"assistant", "agent", "ai"}:
            append_agent_chunks(
                str(m.message_id),
                m.created_at.isoformat(),
                [{"kind": "text", "text": m.content}],
            )
    flush_agent_turn()
    return messages


async def _get_langgraph_messages_for_task(
    session: AsyncSession, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    cp = await load_latest_checkpoint(session, task_id=task_id)
    checkpoint_last_seq = int((cp.blob if cp else {}).get("last_message_seq", 0))
    messages = []
    if cp and cp.blob and "messages" in cp.blob:
        messages.extend(
            [_normalize_langgraph_message(m, i) for i, m in enumerate(cp.blob["messages"])]
        )

    msg_result = await session.execute(
        select(Message)
        .where(Message.task_id == task_id, Message.seq > checkpoint_last_seq)
        .order_by(Message.seq)
    )
    db_messages = list(msg_result.scalars().all())
    for m in db_messages:
        msg_type = (
            "human"
            if m.role in {"human", "user", "user_guidance"}
            else "ai"
            if m.role in {"ai", "assistant", "agent"}
            else "tool"
            if m.role == "tool"
            else "system"
        )
        messages.append(
            {
                "type": msg_type,
                "id": str(m.message_id),
                "content": m.content,
            }
        )
    return messages


def task_to_agent_thread(
    task: Task, parsed_messages: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    messages = parsed_messages or []
    meta = task.metadata_ or {}
    is_resolved = meta.get("resolved") is True
    active_run_id = str(task.active_run_id) if task.active_run_id else None
    viewed_run_id = meta.get("last_viewed_run_id")
    is_viewed = (
        viewed_run_id == active_run_id
        if active_run_id is not None
        else isinstance(meta.get("last_viewed_at_ms"), int | float)
    )
    status = (
        "running"
        if task.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}
        else "finished"
        if task.status == TaskStatus.SUCCEEDED.value
        else "error"
        if task.status == TaskStatus.FAILED.value
        else "interrupted"
        if task.status in {TaskStatus.PARKED.value, TaskStatus.CANCELLED.value}
        else "idle"
    )
    experience = meta.get("experience")
    pull_requests = meta.get("pull_requests")
    return {
        "id": task.thread_id or str(task.task_id),
        "backend": "platform",
        "experience": experience if experience in {"coding", "agent_workspace"} else "coding",
        "title": task.title or f"Task on {task.repo or 'repository'}",
        "repo": task.repo.split("/")[-1] if task.repo else "",
        "repoFullName": task.repo or "",
        "branch": meta.get("head_branch") or task.base_ref or "main",
        "model": task.model,
        "effort": meta.get("agent_effort"),
        "planMode": meta.get("plan_mode") is True,
        "source": (
            "schedule"
            if meta.get("schedule_id")
            else ("dashboard" if task.source == "web_ui" else task.source)
        ),
        "status": status,
        "viewed": is_viewed,
        "viewedAt": meta.get("last_viewed_at_ms"),
        "resolved": is_resolved,
        "createdAt": int(task.created_at.replace(tzinfo=UTC).timestamp() * 1000),
        "updatedAt": int(task.updated_at.replace(tzinfo=UTC).timestamp() * 1000),
        "messages": messages,
        "pullRequests": pull_requests if isinstance(pull_requests, list) else [],
    }


@router.get("/threads/sidebar")
async def get_sidebar_threads(
    db: AsyncSession = Depends(get_db), auth: AuthContext = Depends(get_auth)
) -> dict[str, Any]:
    result = await db.execute(
        select(Task).where(Task.org_id == auth.org_id).order_by(Task.updated_at.desc())
    )
    tasks = list(result.scalars().all())

    active_items = []
    resolved_items = []
    for t in tasks:
        thread = task_to_agent_thread(t)
        if thread["resolved"]:
            resolved_items.append(thread)
        else:
            active_items.append(thread)

    return {
        "active": {"items": active_items, "limit": 100, "hasMore": False},
        "resolved": {"items": resolved_items, "limit": 100, "hasMore": False},
    }


async def _dashboard_tasks(db: AsyncSession, auth: AuthContext) -> list[Task]:
    result = await db.execute(select(Task).where(Task.org_id == auth.org_id))
    return list(result.scalars().all())


def _is_automation(task: Task) -> bool:
    return task.source == "schedule" or bool((task.metadata_ or {}).get("schedule_id"))


@router.get("/threads/projects")
@router.get("/threads/repos")
async def get_thread_projects(
    include_resolved: bool = False,
    include_automations: bool = False,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    experience: Literal["coding", "agent_workspace"] | None = None,
) -> list[dict[str, str | int]]:
    projects: dict[str, dict[str, str | int]] = {}
    tasks = sorted(await _dashboard_tasks(db, auth), key=lambda task: task.updated_at, reverse=True)
    for task in tasks:
        if not task.repo or (not include_resolved and (task.metadata_ or {}).get("resolved")):
            continue
        if not include_automations and _is_automation(task):
            continue
        if experience is not None:
            task_exp = (task.metadata_ or {}).get("experience")
            normalized_exp = task_exp if task_exp in {"coding", "agent_workspace"} else "coding"
            if normalized_exp != experience:
                continue
        projects.setdefault(
            task.repo.casefold(),
            {
                "repoFullName": task.repo,
                "name": task.repo.split("/")[-1],
                "updatedAt": int(task.updated_at.replace(tzinfo=UTC).timestamp() * 1000),
            },
        )
    return list(projects.values())


@router.get("/threads/pinned")
async def get_pinned_threads(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
    experience: Literal["coding", "agent_workspace"] | None = None,
) -> list[dict[str, object]]:
    tasks = sorted(await _dashboard_tasks(db, auth), key=lambda task: task.updated_at, reverse=True)
    items = [
        task_to_agent_thread(task)
        for task in tasks
        if str(auth.user_id) in (task.metadata_ or {}).get("pinned_by", [])
    ]
    if experience is not None:
        items = [thread for thread in items if thread.get("experience") == experience]
    return items


@router.get("/threads/page")
async def get_threads_page(
    limit: int = 50,
    offset: int = 0,
    resolved: bool | None = None,
    viewed: bool | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    scope: Literal["all", "interactive", "automation"] = "all",
    automation_id: str | None = None,
    repo: str | None = None,
    ownerless: bool = False,
    experience: Literal["coding", "agent_workspace"] | None = None,
    sort_by: Literal["created_at", "updated_at"] = "updated_at",
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, object]:
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(422, "limit must be between 1 and 100 and offset must be nonnegative")
    tasks = sorted(
        await _dashboard_tasks(db, auth),
        key=lambda task: task.created_at if sort_by == "created_at" else task.updated_at,
        reverse=True,
    )
    items: list[dict[str, object]] = []
    for task in tasks:
        if repo is not None and (task.repo or "").casefold() != repo.casefold():
            continue
        if ownerless and task.repo:
            continue
        if scope != "all" and _is_automation(task) != (scope == "automation"):
            continue
        if automation_id and (task.metadata_ or {}).get("schedule_id") != automation_id:
            continue
        thread = task_to_agent_thread(task)
        if experience is not None and thread.get("experience") != experience:
            continue
        if resolved is not None and thread["resolved"] != resolved:
            continue
        if viewed is not None and thread["viewed"] != viewed:
            continue
        if source and thread["source"] != source:
            continue
        if status and thread["status"] != status:
            continue
        if q and q.casefold() not in f"{task.title or ''} {task.repo or ''}".casefold():
            continue
        items.append(thread)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "hasMore": offset + limit < len(items),
    }


@router.post("/threads/{thread_id}/pin", status_code=204)
@router.delete("/threads/{thread_id}/pin", status_code=204)
async def set_thread_pin(
    thread_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> Response:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id, for_update=True)
    if task is None:
        raise HTTPException(404, "Task not found")
    metadata = dict(task.metadata_ or {})
    pinned_by = set(metadata.get("pinned_by", []))
    if request.method == "POST":
        pinned_by.add(str(auth.user_id))
    else:
        pinned_by.discard(str(auth.user_id))
    task.metadata_ = {**metadata, "pinned_by": sorted(pinned_by)}
    await db.commit()
    return Response(status_code=204)


@router.get("/threads/{thread_id}")
async def get_thread(
    thread_id: str,
    mark_viewed: bool = True,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id, for_update=True)
    if task is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    if mark_viewed and task.status not in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}:
        meta = dict(task.metadata_ or {})
        meta["last_viewed_at_ms"] = int(datetime.now(UTC).timestamp() * 1000)
        if task.active_run_id:
            meta["last_viewed_run_id"] = str(task.active_run_id)
        task.metadata_ = meta
        await db.flush()

    parsed_messages = await _get_messages_for_task(db, task.task_id)
    return task_to_agent_thread(task, parsed_messages)


@router.get("/threads/{thread_id}/state")
async def get_thread_state(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    if task is None:
        return {"values": {"messages": []}, "next": []}

    messages = await _get_langgraph_messages_for_task(db, task.task_id)
    return {
        "values": {"messages": messages},
        "next": [] if task.status in {"succeeded", "failed", "cancelled"} else ["agent"],
    }


@router.post("/threads/{thread_id}/history")
async def post_thread_history(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    if task is None:
        return []

    messages = await _get_langgraph_messages_for_task(db, task.task_id)
    return [{"values": {"messages": messages}, "checkpoint": {}}]


@router.post("/threads/{thread_id}/commands")
async def post_thread_commands(
    thread_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}

    command_id = body.get("id")
    if not isinstance(command_id, int) or isinstance(command_id, bool):
        return {
            "type": "error",
            "id": None,
            "error": "invalid_argument",
            "message": "command id must be an integer",
        }

    if body.get("method") != "run.start":
        return {
            "type": "error",
            "id": command_id,
            "error": "unknown_command",
            "message": "only run.start is supported",
        }

    params = body.get("params", {})
    if not isinstance(params, dict):
        params = {}
    run_input = params.get("input", {}) or {}
    if not isinstance(run_input, dict):
        run_input = {}
    configurable = _command_configurable(params)

    content = ""
    submitted_message_id = None
    messages = run_input.get("messages", [])
    if messages and isinstance(messages, list):
        submitted_message = messages[-1]
        submitted_message_id = _submitted_message_id(submitted_message)
        content_raw = (
            submitted_message.get("content") if isinstance(submitted_message, dict) else ""
        ) or ""
        if isinstance(content_raw, list):
            text_parts = []
            for block in content_raw:
                if isinstance(block, dict):
                    text_parts.append(block.get("text") or "")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "".join(text_parts)
        else:
            content = str(content_raw)

    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id, for_update=True)

    settings = get_settings()
    profile = await db.get(UserSettings, auth.user_id)
    org = await db.get(OrgSettings, auth.org_id)
    profile_values = dict(profile.settings or {}) if profile else {}
    team_values = dict((org.settings or {}).get("team_settings", {})) if org else {}
    requested_model = configurable.get("agent_model_id")
    model = requested_model if isinstance(requested_model, str) and requested_model else None
    requested_repo = configurable.get("repo")
    repo = requested_repo if isinstance(requested_repo, str) and requested_repo else None
    if configurable.get("repo_explicitly_none") is not True and repo is None:
        default_repo = profile_values.get("default_repo") or team_values.get("default_repo")
        repo = default_repo if isinstance(default_repo, str) and default_repo else None
    base_ref = profile_values.get("base_branch") or "main"
    target_repo = repo or (task.repo if task else None)
    instructions = _agent_instruction_records(org).get(str(target_repo), {}).get("instructions")
    run_metadata: dict[str, Any] = {
        "agent_effort": configurable.get("agent_effort") or profile_values.get("reasoning_effort"),
        "plan_mode": configurable.get("plan_mode") is True,
    }
    if configurable.get("experience") in {"coding", "agent_workspace"}:
        run_metadata["experience"] = configurable["experience"]
    if auth.github_login:
        run_metadata["github_login"] = auth.github_login
    if isinstance(instructions, str) and instructions.strip():
        run_metadata["custom_instructions"] = instructions.strip()
    if task is None:
        from alephat_platform.api.routes_tasks import create_task

        task = await create_task(
            db,
            org_id=auth.org_id,
            user_id=auth.user_id,
            title=content[:100] if content else "New Chat Run",
            prompt=content,
            prompt_message_id=submitted_message_id,
            repo=repo,
            base_ref=base_ref,
            source="web_ui",
            source_ref=None,
            thread_id=thread_id,
            agent_type="coding",
            model=model,
            sandbox_provider=None,
            mcp_server_ids=[],
            mcp_mode="inherit",
            metadata=run_metadata,
            platform_default_model=settings.default_model,
            platform_default_sandbox_provider=settings.default_sandbox_provider,
        )
        await db.commit()
    else:
        if content:
            if model:
                task.model = model
            task.metadata_ = {**(task.metadata_ or {}), **run_metadata}
            was_terminal = is_terminal(task.status)
            if was_terminal or task.status in {TaskStatus.PARKED.value, TaskStatus.QUEUED.value}:
                task.status = TaskStatus.QUEUED.value
                task.park_reason = None
                task.updated_at = datetime.now(UTC)
                await add_message(
                    db,
                    task_id=task.task_id,
                    content=content,
                    kind="user_guidance",
                    message_id=submitted_message_id,
                )
                await enqueue_task_work(
                    db,
                    org_id=task.org_id,
                    task_id=task.task_id,
                    agent_type=task.agent_type,
                    reason="follow_up" if was_terminal else "resumed",
                )
                await db.commit()
            elif task.status == TaskStatus.RUNNING.value:
                await add_message(
                    db,
                    task_id=task.task_id,
                    content=content,
                    kind="user_guidance",
                    message_id=submitted_message_id,
                )
                await db.commit()

    return _protocol_success(command_id, str(task.active_run_id or task.task_id))


@router.post("/threads/{thread_id}/messages")
async def post_thread_message(
    thread_id: str,
    body: DashboardMessageRequest,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id, for_update=True)

    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    should_enqueue = is_terminal(task.status)
    if should_enqueue:
        task.status = TaskStatus.QUEUED.value
        task.park_reason = None
        task.cancel_requested_at = None
        task.cancel_reason = None
        task.updated_at = datetime.now(UTC)

    await add_message(
        db,
        task_id=task.task_id,
        content=body.content,
        kind="user_guidance",
        message_id=body.client_message_id,
    )
    if should_enqueue:
        await enqueue_task_work(
            db,
            org_id=task.org_id,
            task_id=task.task_id,
            agent_type=task.agent_type,
            reason="follow_up_race",
        )
    await db.commit()

    parsed_messages = await _get_messages_for_task(db, task.task_id)
    return task_to_agent_thread(task, parsed_messages)


@router.post("/threads/{thread_id}/cancel")
async def post_cancel_thread(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)

    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    await cancel_task(db, task_id=task.task_id, reason="user_requested")
    await db.commit()

    parsed_messages = await _get_messages_for_task(db, task.task_id)
    return task_to_agent_thread(task, parsed_messages)


class ResolveThreadRequest(BaseModel):
    resolved: bool


@router.post("/threads/{thread_id}/resolve")
async def post_resolve_thread(
    thread_id: str,
    body: ResolveThreadRequest,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    meta = dict(task.metadata_ or {})
    meta["resolved"] = body.resolved
    task.metadata_ = meta
    task.updated_at = datetime.now(UTC)
    await db.commit()

    parsed_messages = await _get_messages_for_task(db, task.task_id)
    return task_to_agent_thread(task, parsed_messages)


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> None:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    await db.delete(task)
    await db.commit()


@router.get("/threads/{thread_id}/stream")
@router.post("/threads/{thread_id}/stream/events")
async def stream_thread(
    thread_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    requested_channels = set(body.get("channels") or {"values", "lifecycle"})

    async def gen():
        factory = __import__(
            "alephat_platform.common.db", fromlist=["get_session_factory"]
        ).get_session_factory()

        yield ": keep-alive\n\n"
        last_msg_json = None
        announced_running = False
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                return

            payloads: list[dict[str, Any]] = []
            async with factory() as session:
                task = await _get_task_by_id_or_thread_id(session, auth.org_id, thread_id)
                if task is not None and task.org_id == auth.org_id:
                    is_running = task.status in {
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                    }
                    if "lifecycle" in requested_channels and is_running and not announced_running:
                        payloads.append(_protocol_event("lifecycle", {"event": "running"}))
                        announced_running = True

                    if "values" in requested_channels:
                        messages = await _get_langgraph_messages_for_task(session, task.task_id)
                        msg_json = json.dumps(messages, sort_keys=True)
                        if msg_json != last_msg_json:
                            last_msg_json = msg_json
                            payloads.append(_protocol_event("values", {"messages": messages}))

                    lifecycle = _terminal_lifecycle(task.status)
                    if lifecycle is not None and announced_running:
                        announced_running = False
                        if "lifecycle" in requested_channels:
                            payloads.append(_protocol_event("lifecycle", lifecycle))

            if payloads:
                idle_ticks = 0
                for payload in payloads:
                    yield _sse_data(payload)
            else:
                idle_ticks += 1
                if idle_ticks >= 40:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/workflow-approval/{thread_id}")
async def list_thread_workflow_approvals(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)

    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(Approval).where(
            Approval.task_id == task.task_id, Approval.status == ApprovalStatus.PENDING.value
        )
    )
    approvals = list(result.scalars().all())

    return {
        "approvals": [
            {
                "approval_id": str(a.approval_id),
                "kind": a.kind,
                "status": a.status,
                "payload": a.payload,
                "created_at": a.created_at.isoformat(),
            }
            for a in approvals
        ]
    }


@router.post("/workflow-approval/{thread_id}/{fingerprint}/approve")
async def approve_workflow_push_fingerprint(
    thread_id: str,
    fingerprint: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)

    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(Approval).where(
            Approval.task_id == task.task_id, Approval.status == ApprovalStatus.PENDING.value
        )
    )
    approvals = list(result.scalars().all())
    target_approval = None
    for a in approvals:
        if a.payload.get("fingerprint") == fingerprint or str(a.approval_id) == fingerprint:
            target_approval = a
            break

    if target_approval is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Pending approval not found")

    await decide_approval(
        db, approval_id=target_approval.approval_id, user_id=auth.user_id, decision="approved"
    )
    await db.commit()
    return {"status": "approved", "fingerprint": fingerprint}


@router.post("/workflow-approval/{thread_id}/{fingerprint}/reject")
async def reject_workflow_push_fingerprint(
    thread_id: str,
    fingerprint: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)

    if task is None or task.org_id != auth.org_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(Approval).where(
            Approval.task_id == task.task_id, Approval.status == ApprovalStatus.PENDING.value
        )
    )
    approvals = list(result.scalars().all())
    target_approval = None
    for a in approvals:
        if a.payload.get("fingerprint") == fingerprint or str(a.approval_id) == fingerprint:
            target_approval = a
            break

    if target_approval is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Pending approval not found")

    await decide_approval(
        db, approval_id=target_approval.approval_id, user_id=auth.user_id, decision="rejected"
    )
    await db.commit()
    return {"status": "rejected", "fingerprint": fingerprint}


async def _github_repo_headers(repo_name: str) -> dict[str, str]:
    from openswe.github.app import get_github_app_installation_token

    token = await get_github_app_installation_token(
        repositories=[repo_name] if repo_name else None,
        log_errors=False,
    )
    if not token:
        raise HTTPException(status_code=503, detail="GitHub App credentials are not configured")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _fetch_github_pr(owner: str, repo: str, number: int) -> dict[str, Any]:
    headers = await _github_repo_headers(repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Pull request not found")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected GitHub PR response")
    return data


async def _fetch_github_pr_files(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    headers = await _github_repo_headers(repo)
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers, params={"per_page": 100})
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


@router.get("/threads/{thread_id}/pr-diff")
@router.get("/threads/{thread_id}/branch-diff")
async def get_thread_pr_diff(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    meta = dict((task.metadata_ if task else None) or {})
    pr_number = meta.get("pr_number")
    base_ref = (task.base_ref if task else None) or "main"
    head_ref = meta.get("head_branch") or base_ref
    if task and task.repo and isinstance(pr_number, int):
        owner, _, repo = task.repo.partition("/")
        try:
            pr_data = await _fetch_github_pr(owner, repo, pr_number)
            raw_files = await _fetch_github_pr_files(owner, repo, pr_number)
            base_sha = str((pr_data.get("base") or {}).get("sha") or "base")
            head_sha = str((pr_data.get("head") or {}).get("sha") or "head")
            return {
                "prNumber": pr_number,
                "baseRef": str((pr_data.get("base") or {}).get("ref") or base_ref),
                "headRef": str((pr_data.get("head") or {}).get("ref") or head_ref),
                "baseSha": base_sha,
                "headSha": head_sha,
                "truncated": False,
                "files": [
                    {
                        "path": str(f["filename"]),
                        "previousPath": f.get("previous_filename"),
                        "status": str(f.get("status") or "modified"),
                        "additions": int(f.get("additions") or 0),
                        "deletions": int(f.get("deletions") or 0),
                        "originalContent": None,
                        "modifiedContent": None,
                        "patch": f.get("patch"),
                        "unrenderable": f.get("patch") is None,
                    }
                    for f in raw_files
                    if isinstance(f.get("filename"), str)
                ],
            }
        except Exception:
            pass
    return {
        "prNumber": pr_number if isinstance(pr_number, int) else 1,
        "baseRef": base_ref,
        "headRef": head_ref,
        "baseSha": "base",
        "headSha": "head",
        "truncated": False,
        "files": [],
    }


@router.get("/repos/{owner}/{repo}/pulls/{number}")
async def get_repo_pull_request_details(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    pr = await _fetch_github_pr(owner, repo, number)
    state = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
    return {
        "repo": f"{owner}/{repo}",
        "number": number,
        "title": str(pr.get("title") or f"PR #{number}"),
        "state": state if state in {"open", "closed", "merged"} else "open",
        "draft": bool(pr.get("draft")),
        "additions": int(pr.get("additions") or 0),
        "deletions": int(pr.get("deletions") or 0),
        "mergeable": pr.get("mergeable"),
        "mergeState": str(pr.get("mergeable_state") or "clean"),
        "autoMerge": pr.get("auto_merge") is not None,
        "headSha": (pr.get("head") or {}).get("sha"),
        "headRef": (pr.get("head") or {}).get("ref"),
        "reviewDecision": "none",
        "reviewers": [],
        "reviewRequired": False,
        "statusAvailable": True,
        "createdAt": pr.get("created_at"),
        "updatedAt": pr.get("updated_at"),
        "ci": "none",
        "requiredChecksPassing": True,
        "failingChecks": [],
        "pendingChecks": [],
        "missingChecks": [],
        "unresolvedThreads": 0,
    }


@router.get("/repos/{owner}/{repo}/pulls/{number}/thread")
async def get_repo_pull_request_thread(
    owner: str,
    repo: str,
    number: int,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    full_name = f"{owner}/{repo}"
    tasks = sorted(await _dashboard_tasks(db, auth), key=lambda t: t.updated_at, reverse=True)
    for task in tasks:
        if (task.repo or "").casefold() != full_name.casefold():
            continue
        meta = dict(task.metadata_ or {})
        if meta.get("pr_number") == number:
            return {
                "thread_id": task.thread_id or str(task.task_id),
                "already_running": task.status
                in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value},
            }
    return {"thread_id": "", "already_running": False}


@router.get("/reviews/{owner}/{repo}/{number}")
async def get_review_detail(
    owner: str,
    repo: str,
    number: int,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del db, auth
    pr = await _fetch_github_pr(owner, repo, number)
    raw_user = pr.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    raw_state = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
    pr_state = "draft" if pr.get("draft") and raw_state == "open" else raw_state
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    head_ref = str((pr.get("head") or {}).get("ref") or "")
    base_ref = str((pr.get("base") or {}).get("ref") or "main")
    return {
        "thread_id": None,
        "trace_url": None,
        "owner": owner,
        "repo": repo,
        "full_name": f"{owner}/{repo}",
        "number": number,
        "title": str(pr.get("title") or f"PR #{number}"),
        "url": str(pr.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"),
        "head_ref": head_ref,
        "base_ref": base_ref,
        "author": str(user.get("login") or ""),
        "head_sha": head_sha,
        "watch": False,
        "status": "none",
        "counts": {"open": 0, "resolved": 0, "dismissed": 0, "bugs": 0, "flags": 0},
        "updated_at": pr.get("updated_at"),
        "assessment": None,
        "pr": {
            "state": pr_state,
            "title": str(pr.get("title") or ""),
            "body": str(pr.get("body") or ""),
            "description_pull_requests": [],
            "additions": int(pr.get("additions") or 0),
            "deletions": int(pr.get("deletions") or 0),
            "changed_files": int(pr.get("changed_files") or 0),
            "commits": int(pr.get("commits") or 0),
            "head_sha": head_sha,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "author": (
                {"login": str(user.get("login") or ""), "avatar_url": user.get("avatar_url")}
                if user
                else None
            ),
            "created_at": pr.get("created_at"),
            "merged_at": pr.get("merged_at"),
            "assignees": [
                {"login": str(a.get("login") or ""), "avatar_url": a.get("avatar_url")}
                for a in (pr.get("assignees") or [])
                if isinstance(a, dict)
            ],
            "requested_reviewers": [
                {"login": str(r.get("login") or ""), "avatar_url": r.get("avatar_url")}
                for r in (pr.get("requested_reviewers") or [])
                if isinstance(r, dict)
            ],
            "labels": [
                {"name": str(lbl.get("name") or ""), "color": lbl.get("color")}
                for lbl in (pr.get("labels") or [])
                if isinstance(lbl, dict)
            ],
        },
        "checks": [],
        "findings": [],
        "walkthrough": None,
        "walkthrough_running": False,
        "walkthrough_error": None,
        "walkthrough_scout_thread_id": None,
        "walkthrough_progress": None,
        "review_error": None,
    }


@router.get("/reviews/{owner}/{repo}/{number}/diff")
async def get_review_diff(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    pr = await _fetch_github_pr(owner, repo, number)
    raw_files = await _fetch_github_pr_files(owner, repo, number)
    base_sha = str((pr.get("base") or {}).get("sha") or "")
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    files = [
        {
            "baseSha": base_sha,
            "headSha": head_sha,
            "path": str(f["filename"]),
            "previousPath": f.get("previous_filename"),
            "status": (
                f["status"]
                if f.get("status") in {"added", "removed", "modified", "renamed"}
                else "modified"
            ),
            "additions": int(f.get("additions") or 0),
            "deletions": int(f.get("deletions") or 0),
            "patch": f.get("patch"),
            "unrenderable": f.get("patch") is None,
        }
        for f in raw_files
        if isinstance(f.get("filename"), str)
    ]
    return {
        "files": files,
        "total_additions": sum(int(f.get("additions") or 0) for f in raw_files),
        "total_deletions": sum(int(f.get("deletions") or 0) for f in raw_files),
        "truncated": False,
    }


@router.get("/reviews/{owner}/{repo}/{number}/file-contents")
async def get_review_file_contents(
    owner: str,
    repo: str,
    number: int,
    path: str,
    base_sha: str,
    head_sha: str,
    status: str = "modified",
    previous_path: str | None = None,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str | None]:
    del number, auth
    headers = {**(await _github_repo_headers(repo)), "Accept": "application/vnd.github.raw+json"}
    original_content: str | None = None
    modified_content: str | None = None
    async with httpx.AsyncClient(timeout=20.0) as client:
        if status != "added" and base_sha:
            orig_path = previous_path or path
            r_orig = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{orig_path}",
                headers=headers,
                params={"ref": base_sha},
            )
            if r_orig.status_code == 200:
                original_content = r_orig.text
        if status != "removed" and head_sha:
            r_mod = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": head_sha},
            )
            if r_mod.status_code == 200:
                modified_content = r_mod.text
    return {"originalContent": original_content, "modifiedContent": modified_content}


@router.get("/reviews/{owner}/{repo}/{number}/conversation")
async def get_review_conversation(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    headers = await _github_repo_headers(repo)
    items: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        commits_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/commits",
            headers=headers,
            params={"per_page": 100},
        )
        if commits_resp.status_code == 200 and isinstance(commits_resp.json(), list):
            for c in commits_resp.json():
                if not isinstance(c, dict):
                    continue
                author_user = c.get("author") if isinstance(c.get("author"), dict) else None
                raw_commit = c.get("commit")
                commit_obj: dict[str, Any] = raw_commit if isinstance(raw_commit, dict) else {}
                raw_committer = commit_obj.get("author")
                committer_info: dict[str, Any] = (
                    raw_committer if isinstance(raw_committer, dict) else {}
                )
                items.append(
                    {
                        "kind": "commit",
                        "sha": str(c.get("sha") or ""),
                        "author": (
                            {
                                "login": str(author_user.get("login") or ""),
                                "avatar_url": str(author_user.get("avatar_url") or ""),
                                "bot": str(author_user.get("type") or "").lower() == "bot",
                            }
                            if author_user
                            else None
                        ),
                        "created_at": str(committer_info.get("date") or ""),
                        "message": str(commit_obj.get("message") or ""),
                        "html_url": str(c.get("html_url") or ""),
                    }
                )
        comments_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
            headers=headers,
            params={"per_page": 100},
        )
        if comments_resp.status_code == 200 and isinstance(comments_resp.json(), list):
            for cm in comments_resp.json():
                if not isinstance(cm, dict):
                    continue
                u = cm.get("user") if isinstance(cm.get("user"), dict) else None
                items.append(
                    {
                        "kind": "comment",
                        "id": int(cm.get("id") or 0),
                        "author": (
                            {
                                "login": str(u.get("login") or ""),
                                "avatar_url": str(u.get("avatar_url") or ""),
                                "bot": str(u.get("type") or "").lower() == "bot",
                            }
                            if u
                            else None
                        ),
                        "created_at": str(cm.get("created_at") or ""),
                        "body": str(cm.get("body") or ""),
                        "html_url": str(cm.get("html_url") or ""),
                    }
                )
    return {"items": items, "threads": []}


@router.get("/reviews/{owner}/{repo}/{number}/pending-review")
async def get_pending_review(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del owner, repo, number, auth
    return {"id": 0, "node_id": "", "comments": []}


@router.get("/reviews/{owner}/{repo}/{number}/chat")
async def get_review_chat_meta(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del owner, repo, number, auth
    return {"available": False, "assistant_id": "agent", "thread_id": ""}


@router.post("/reviews/{owner}/{repo}/{number}/viewed", status_code=204)
async def post_review_viewed(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> Response:
    del owner, repo, number, auth
    return Response(status_code=204)


@router.get("/reviews/{owner}/{repo}/{number}/preview")
async def get_review_preview(
    owner: str,
    repo: str,
    number: int,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    pr = await _fetch_github_pr(owner, repo, number)
    raw_files = await _fetch_github_pr_files(owner, repo, number)
    raw_user = pr.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    return {
        "title": str(pr.get("title") or ""),
        "body": str(pr.get("body") or ""),
        "author": user.get("login"),
        "author_avatar_url": user.get("avatar_url"),
        "state": "merged" if pr.get("merged_at") else str(pr.get("state") or "open"),
        "draft": bool(pr.get("draft")),
        "head_ref": str((pr.get("head") or {}).get("ref") or ""),
        "base_ref": str((pr.get("base") or {}).get("ref") or "main"),
        "commits": int(pr.get("commits") or 0),
        "additions": int(pr.get("additions") or 0),
        "deletions": int(pr.get("deletions") or 0),
        "changed_files": int(pr.get("changed_files") or 0),
        "files": [
            {
                "path": str(f["filename"]),
                "status": str(f.get("status") or "modified"),
                "additions": int(f.get("additions") or 0),
                "deletions": int(f.get("deletions") or 0),
            }
            for f in raw_files
            if isinstance(f.get("filename"), str)
        ],
        "unresolved": [],
        "checks": [],
        "human_input": "",
    }


@router.get("/threads/{thread_id}/working-tree-diff")
async def get_thread_working_tree_diff(
    thread_id: str,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del thread_id, auth
    return {
        "status": "ready",
        "truncated": False,
        "summary": {"files": 0, "additions": 0, "deletions": 0},
        "files": [],
    }


@router.get("/threads/{thread_id}/pull-request-status")
async def get_thread_pull_request_status(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id)
    meta = dict((task.metadata_ if task else None) or {})
    prs = meta.get("pull_requests")
    if not isinstance(prs, list):
        return {"pullRequests": []}
    return {
        "pullRequests": [
            {
                "repoFullName": pr.get("repoFullName"),
                "number": pr.get("number"),
                "url": pr.get("url"),
                "statusAvailable": True,
                "state": "open" if pr.get("state") in {"draft", "open"} else pr.get("state"),
                "isDraft": pr.get("state") == "draft",
                "mergeConflictState": "mergeable",
                "checksAvailable": True,
                "failingChecks": [],
                "pendingCheckCount": 0,
                "inconclusiveCheckCount": 0,
                "commentsAvailable": True,
                "unresolvedReviewThreadCount": 0,
                "unresolvedReviewThreads": [],
            }
            for pr in prs
            if isinstance(pr, dict)
        ]
    }


@router.post("/threads/pull-request-checks")
async def post_pull_request_checks(
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    return {}


@router.post("/threads/{thread_id}/viewed")
async def post_thread_viewed(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    task = await _get_task_by_id_or_thread_id(db, auth.org_id, thread_id, for_update=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    viewed_at = int(datetime.now(UTC).timestamp() * 1000)
    meta = dict(task.metadata_ or {})
    meta["last_viewed_at_ms"] = viewed_at
    if task.active_run_id:
        meta["last_viewed_run_id"] = str(task.active_run_id)
    task.metadata_ = meta
    await db.commit()
    return {
        "runId": str(task.active_run_id) if task.active_run_id else None,
        "viewed": True,
        "viewedAt": viewed_at,
    }


@router.get("/threads/{thread_id}/queued-messages")
async def get_thread_queued_messages(
    thread_id: str,
    auth: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    del thread_id, auth
    return []


@router.get("/threads/{thread_id}/runs")
async def get_thread_runs(
    thread_id: str,
    auth: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    del thread_id, auth
    return []


@router.get("/threads/{thread_id}/background-work")
async def get_thread_background_work(
    thread_id: str,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, list[Any]]:
    del thread_id, auth
    return {"commands": [], "monitors": []}


@router.get("/analytics/config")
async def get_analytics_config() -> dict[str, str]:
    return {
        "environment": "local",
        "posthog_api_key": "",
        "posthog_host": "",
    }


@router.get("/workspaces/options")
async def get_workspaces_options(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    org = await db.get(OrgSettings, auth.org_id)
    team_values = dict((org.settings or {}).get("team_settings", {})) if org else {}
    default_repo = team_values.get("default_repo")
    return {
        "workspaces": [
            {
                "slug": "default",
                "name": "Default",
                "repos": [],
                "default_repo": default_repo if isinstance(default_repo, str) else None,
                "slack_channel_ids": [],
                "kitchen_channel_ids": [],
                "is_default": True,
                "has_snapshot": False,
            }
        ],
        "default_slug": "default",
    }


class UserPreferencesBody(BaseModel):
    default_visibility: Literal["public", "private"] = "public"
    default_workspace: str | None = "default"
    follow_up_behavior: Literal["queue", "steer"] = "steer"


@router.get("/me/preferences")
async def get_my_preferences(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    row = await db.get(UserSettings, auth.user_id)
    prefs = dict((row.settings or {}).get("preferences", {})) if row else {}
    return {
        "default_visibility": prefs.get("default_visibility", "public"),
        "default_workspace": prefs.get("default_workspace", "default"),
        "follow_up_behavior": prefs.get("follow_up_behavior", "steer"),
    }


@router.put("/me/preferences")
async def put_my_preferences(
    body: UserPreferencesBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    row = await db.get(UserSettings, auth.user_id)
    if row is None:
        row = UserSettings(user_id=auth.user_id, settings={})
        db.add(row)
    settings_dict = dict(row.settings or {})
    prefs = body.model_dump()
    settings_dict["preferences"] = prefs
    row.settings = settings_dict
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return prefs


class SkillCreateBody(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""


class SkillUpdateBody(BaseModel):
    description: str = ""
    instructions: str = ""


def _stored_skills(values: dict[str, Any] | None, key: str) -> dict[str, dict[str, str]]:
    raw = (values or {}).get(key)
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): {
            "name": str(v.get("name") or k),
            "description": str(v.get("description") or ""),
            "instructions": str(v.get("instructions") or ""),
            "created_at": str(v.get("created_at") or ""),
            "updated_at": str(v.get("updated_at") or ""),
        }
        for k, v in raw.items()
        if isinstance(v, dict)
    }


@router.get("/skills")
async def list_skills(
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    row = await db.get(UserSettings, auth.user_id)
    records = sorted(
        _stored_skills(row.settings if row else None, "skills").values(),
        key=lambda item: item["name"].casefold(),
    )
    sliced = records[offset : offset + limit]
    next_offset = offset + limit if offset + limit < len(records) else None
    return {"items": sliced, "next_offset": next_offset}


@router.post("/skills")
async def create_skill(
    body: SkillCreateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Skill name is required")
    row = await db.get(UserSettings, auth.user_id)
    if row is None:
        row = UserSettings(user_id=auth.user_id, settings={})
        db.add(row)
    settings_dict = dict(row.settings or {})
    skills = _stored_skills(settings_dict, "skills")
    if name in skills:
        raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")
    now = datetime.now(UTC).isoformat()
    record = {
        "name": name,
        "description": body.description,
        "instructions": body.instructions,
        "created_at": now,
        "updated_at": now,
    }
    skills[name] = record
    settings_dict["skills"] = skills
    row.settings = settings_dict
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.put("/skills/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    row = await db.get(UserSettings, auth.user_id)
    if row is None:
        row = UserSettings(user_id=auth.user_id, settings={})
        db.add(row)
    settings_dict = dict(row.settings or {})
    skills = _stored_skills(settings_dict, "skills")
    existing = skills.get(name)
    now = datetime.now(UTC).isoformat()
    record = {
        "name": name,
        "description": body.description,
        "instructions": body.instructions,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    skills[name] = record
    settings_dict["skills"] = skills
    row.settings = settings_dict
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(
    name: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> Response:
    row = await db.get(UserSettings, auth.user_id)
    if row is not None:
        settings_dict = dict(row.settings or {})
        skills = _stored_skills(settings_dict, "skills")
        if name in skills:
            del skills[name]
            settings_dict["skills"] = skills
            row.settings = settings_dict
            row.updated_at = datetime.now(UTC)
            await db.flush()
    return Response(status_code=204)


@router.get("/organization-skills")
async def list_organization_skills(
    limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del limit, cursor
    org = await db.get(OrgSettings, auth.org_id)
    records = sorted(
        _stored_skills(org.settings if org else None, "organization_skills").values(),
        key=lambda item: item["name"].casefold(),
    )
    return {"items": records, "next_cursor": None}


@router.post("/organization-skills")
async def create_organization_skill(
    body: SkillCreateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Skill name is required")
    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    skills = _stored_skills(settings_dict, "organization_skills")
    if name in skills:
        raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")
    now = datetime.now(UTC).isoformat()
    record = {
        "name": name,
        "description": body.description,
        "instructions": body.instructions,
        "created_at": now,
        "updated_at": now,
    }
    skills[name] = record
    settings_dict["organization_skills"] = skills
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.put("/organization-skills/{name}")
async def update_organization_skill(
    name: str,
    body: SkillUpdateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    skills = _stored_skills(settings_dict, "organization_skills")
    existing = skills.get(name)
    now = datetime.now(UTC).isoformat()
    record = {
        "name": name,
        "description": body.description,
        "instructions": body.instructions,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    skills[name] = record
    settings_dict["organization_skills"] = skills
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.delete("/organization-skills/{name}", status_code=204)
async def delete_organization_skill(
    name: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> Response:
    org = await db.get(OrgSettings, auth.org_id)
    if org is not None:
        settings_dict = dict(org.settings or {})
        skills = _stored_skills(settings_dict, "organization_skills")
        if name in skills:
            del skills[name]
            settings_dict["organization_skills"] = skills
            org.settings = settings_dict
            org.updated_at = datetime.now(UTC)
            await db.flush()
    return Response(status_code=204)


class ScheduleCreateBody(BaseModel):
    prompt: str
    triggers: list[dict[str, Any]] = []
    name: str | None = None
    admin_thread: bool = False
    model_id: str | None = None
    effort: str | None = None
    workspace: str = "default"


class ScheduleUpdateBody(BaseModel):
    prompt: str | None = None
    triggers: list[dict[str, Any]] | None = None
    name: str | None = None
    admin_thread: bool | None = None
    model_id: str | None = None
    effort: str | None = None
    enabled: bool | None = None
    workspace: str | None = None


def _stored_schedules(org: OrgSettings | None) -> dict[str, dict[str, Any]]:
    raw = (org.settings or {}).get("schedules") if org else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _normalize_triggers(triggers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    normalized: list[dict[str, Any]] = []
    cron_value: str | None = None
    for item in triggers:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or uuid.uuid4())
        entry = {**item, "id": tid}
        if entry.get("kind") == "schedule" and isinstance(entry.get("cron"), str):
            cron_value = cron_value or entry["cron"]
        normalized.append(entry)
    return normalized, cron_value


@router.get("/schedules")
async def list_schedules(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    org = await db.get(OrgSettings, auth.org_id)
    records = list(_stored_schedules(org).values())
    return sorted(records, key=lambda item: str(item.get("createdAt") or ""), reverse=True)


@router.post("/schedules")
async def create_schedule(
    body: ScheduleCreateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    schedules = _stored_schedules(org)
    schedule_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    triggers, cron_value = _normalize_triggers(body.triggers)
    default_model = org.default_model or get_settings().default_model
    record: dict[str, Any] = {
        "id": schedule_id,
        "name": (body.name or "").strip() or body.prompt.strip()[:60] or "Automation",
        "prompt": body.prompt,
        "schedule": cron_value,
        "triggers": triggers,
        "scope": "workspace",
        "workspace": body.workspace or "default",
        "adminThread": body.admin_thread,
        "model": body.model_id or default_model,
        "effort": body.effort or "medium",
        "enabled": True,
        "cronId": None,
        "lastThreadId": None,
        "lastRunId": None,
        "lastTriggeredAt": None,
        "lastError": None,
        "lastErrorAt": None,
        "createdBy": auth.github_login or "moule3053",
        "updatedBy": auth.github_login or "moule3053",
        "createdAt": now,
        "updatedAt": now,
    }
    schedules[schedule_id] = record
    settings_dict["schedules"] = schedules
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdateBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    schedules = _stored_schedules(org)
    record = schedules.get(schedule_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if body.name is not None:
        record["name"] = body.name.strip() or record["name"]
    if body.prompt is not None:
        record["prompt"] = body.prompt
    if body.triggers is not None:
        triggers, cron_value = _normalize_triggers(body.triggers)
        record["triggers"] = triggers
        record["schedule"] = cron_value
    if body.admin_thread is not None:
        record["adminThread"] = body.admin_thread
    if body.model_id is not None:
        record["model"] = body.model_id
    if body.effort is not None:
        record["effort"] = body.effort
    if body.enabled is not None:
        record["enabled"] = body.enabled
    if body.workspace is not None:
        record["workspace"] = body.workspace
    record["updatedBy"] = auth.github_login or "moule3053"
    record["updatedAt"] = datetime.now(UTC).isoformat()
    schedules[schedule_id] = record
    settings_dict["schedules"] = schedules
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return record


@router.post("/schedules/{schedule_id}/trigger")
async def trigger_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    from alephat_platform.api.routes_tasks import create_task

    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    schedules = _stored_schedules(org)
    record = schedules.get(schedule_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    thread_id = str(uuid.uuid4())
    repo = next(
        (
            str(t.get("repo"))
            for t in (record.get("triggers") or [])
            if isinstance(t, dict) and isinstance(t.get("repo"), str) and t.get("repo")
        ),
        None,
    )
    settings = get_settings()
    task = await create_task(
        db,
        org_id=auth.org_id,
        user_id=auth.user_id,
        title=str(record.get("name") or "Scheduled Run")[:100],
        prompt=str(record.get("prompt") or ""),
        prompt_message_id=None,
        repo=repo,
        base_ref="main",
        source="web_ui",
        source_ref=schedule_id,
        thread_id=thread_id,
        agent_type="coding",
        model=record.get("model"),
        sandbox_provider=None,
        mcp_server_ids=[],
        mcp_mode="inherit",
        metadata={
            "schedule_id": schedule_id,
            "agent_effort": record.get("effort"),
            "experience": "coding" if repo else "agent_workspace",
        },
        platform_default_model=settings.default_model,
        platform_default_sandbox_provider=settings.default_sandbox_provider,
    )
    now = datetime.now(UTC).isoformat()
    run_id = str(task.active_run_id or task.task_id)
    record["lastThreadId"] = thread_id
    record["lastRunId"] = run_id
    record["lastTriggeredAt"] = now
    record["updatedAt"] = now
    schedules[schedule_id] = record
    settings_dict["schedules"] = schedules
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.commit()
    return {
        "status": "started",
        "schedule_id": schedule_id,
        "thread_id": thread_id,
        "run_id": run_id,
    }


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> Response:
    org = await db.get(OrgSettings, auth.org_id)
    if org is not None:
        settings_dict = dict(org.settings or {})
        schedules = _stored_schedules(org)
        if schedule_id in schedules:
            del schedules[schedule_id]
            settings_dict["schedules"] = schedules
            org.settings = settings_dict
            org.updated_at = datetime.now(UTC)
            await db.flush()
    return Response(status_code=204)


def _github_pr_item_to_open_pr(full_name: str, pr: dict[str, Any]) -> dict[str, Any]:
    state = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
    return {
        "repo": full_name,
        "number": int(pr.get("number") or 0),
        "title": str(pr.get("title") or ""),
        "state": state if state in {"open", "closed", "merged"} else "open",
        "draft": bool(pr.get("draft")),
        "additions": pr.get("additions") if isinstance(pr.get("additions"), int) else None,
        "deletions": pr.get("deletions") if isinstance(pr.get("deletions"), int) else None,
        "mergeable": pr.get("mergeable") if isinstance(pr.get("mergeable"), bool) else True,
        "mergeState": str(pr.get("mergeable_state") or "clean"),
        "autoMerge": pr.get("auto_merge") is not None,
        "headSha": (pr.get("head") or {}).get("sha"),
        "headRef": (pr.get("head") or {}).get("ref"),
        "reviewDecision": "none",
        "reviewers": [],
        "reviewRequired": False,
        "statusAvailable": True,
        "createdAt": pr.get("created_at"),
        "updatedAt": pr.get("updated_at"),
        "ci": "none",
        "requiredChecksPassing": True,
        "failingChecks": [],
        "pendingChecks": [],
        "missingChecks": [],
        "unresolvedThreads": 0,
    }


@router.get("/pull-requests")
async def list_open_pull_requests_route(
    repo: str = "",
    lightweight: bool = True,
    sort: str = "updated",
    direction: str = "desc",
    page: int = 1,
    scope: str = "mine",
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del lightweight, page
    login = (auth.github_login or "moule3053").casefold()
    requested_repos = [r.strip() for r in repo.split(",") if r.strip()]
    if not requested_repos:
        try:
            _, installed = await _fetch_installed_github_repositories()
            requested_repos = [
                str(item["full_name"])
                for item in installed
                if isinstance(item.get("full_name"), str) and item["full_name"]
            ]
        except Exception:
            requested_repos = []

    pull_requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for full_name in requested_repos:
            owner, sep, repo_name = full_name.partition("/")
            if not sep or not owner or not repo_name:
                continue
            try:
                headers = await _github_repo_headers(repo_name)
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                    headers=headers,
                    params={
                        "state": "open",
                        "sort": "created" if sort == "created" else "updated",
                        "direction": "asc" if direction == "asc" else "desc",
                        "per_page": 30,
                    },
                )
                if resp.status_code != 200 or not isinstance(resp.json(), list):
                    continue
                for pr in resp.json():
                    if not isinstance(pr, dict):
                        continue
                    raw_user = pr.get("user")
                    user_dict: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
                    author_login = str(user_dict.get("login") or "").casefold()
                    if scope == "review-requested":
                        reviewers = [
                            str(r.get("login") or "").casefold()
                            for r in (pr.get("requested_reviewers") or [])
                            if isinstance(r, dict)
                        ]
                        if login not in reviewers:
                            continue
                    elif scope == "mine" and author_login and author_login != login:
                        # Include both user's PRs and bot-opened PRs in the user's repos
                        if not author_login.endswith("[bot]") and owner.casefold() != login:
                            continue
                    pull_requests.append(_github_pr_item_to_open_pr(full_name, pr))
            except Exception:
                continue

    return {
        "pullRequests": pull_requests,
        "nextPage": None,
        "incomplete": False,
        "updatedAt": datetime.now(UTC).isoformat(),
    }


@router.get("/review-assignments")
async def list_review_assignments(
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    return {
        "pullRequests": [],
        "nextPage": None,
        "incomplete": False,
        "updatedAt": datetime.now(UTC).isoformat(),
    }


@router.get("/pull-requests/search")
async def search_pull_requests_route(
    q: str = "",
    offset: int = 0,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del offset
    listing = await list_open_pull_requests_route(auth=auth)
    query_lower = q.strip().casefold()
    results = []
    for pr in listing.get("pullRequests", []):
        if not isinstance(pr, dict):
            continue
        haystack = f"{pr.get('repo', '')} #{pr.get('number', '')} {pr.get('title', '')}".casefold()
        if not query_lower or query_lower in haystack:
            results.append(
                {
                    "repo": pr["repo"],
                    "number": pr["number"],
                    "url": f"https://github.com/{pr['repo']}/pull/{pr['number']}",
                    "title": pr["title"],
                    "state": "draft" if pr.get("draft") else pr.get("state", "open"),
                }
            )
    return {"pull_requests": results, "has_more": False}


class ReviewSummariesBody(BaseModel):
    pullRequests: list[dict[str, Any]] = []


@router.post("/reviews/summaries")
async def post_review_summaries(
    body: ReviewSummariesBody,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del auth
    return {
        f"{item.get('repo')}#{item.get('number')}": None
        for item in body.pullRequests
        if isinstance(item, dict) and item.get("repo") and item.get("number") is not None
    }


class EnabledReviewRepoBody(BaseModel):
    full_name: str
    enabled: bool


@router.get("/enabled-review-repos")
async def get_enabled_review_repos(
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, list[str]]:
    org = await db.get(OrgSettings, auth.org_id)
    raw = (org.settings or {}).get("enabled_review_repos") if org else None
    repos = [str(r) for r in raw if isinstance(r, str)] if isinstance(raw, list) else []
    return {"repos": sorted(set(repos))}


@router.put("/enabled-review-repos")
async def put_enabled_review_repos(
    body: EnabledReviewRepoBody,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, list[str]]:
    org = await _get_or_create_org_settings(db, auth.org_id)
    settings_dict = dict(org.settings or {})
    raw = settings_dict.get("enabled_review_repos")
    repos = {str(r) for r in raw if isinstance(r, str)} if isinstance(raw, list) else set()
    if body.enabled:
        repos.add(body.full_name)
    else:
        repos.discard(body.full_name)
    sorted_repos = sorted(repos)
    settings_dict["enabled_review_repos"] = sorted_repos
    org.settings = settings_dict
    org.updated_at = datetime.now(UTC)
    await db.flush()
    return {"repos": sorted_repos}


@router.get("/repos/{owner}/{repo}/merge-methods")
async def get_repo_merge_methods(
    owner: str,
    repo: str,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, list[str]]:
    del owner, repo, auth
    return {"mergeMethods": ["squash", "merge", "rebase"]}


@router.get("/ui-invalidations")
async def stream_ui_invalidations(request: Request) -> StreamingResponse:
    async def gen():
        yield 'event: hello\ndata: {"backend_commit":null,"invalidated":[],"denied":[]}\n\n'
        while not await request.is_disconnected():
            await asyncio.sleep(10.0)
            if await request.is_disconnected():
                break
            yield "event: alive\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
