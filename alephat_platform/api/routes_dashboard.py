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
    return {
        "login": auth.github_login or "moule3053",
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
    return {
        "id": task.thread_id or str(task.task_id),
        "backend": "platform",
        "experience": experience if experience in {"coding", "agent_workspace"} else "coding",
        "title": task.title or f"Task on {task.repo or 'repository'}",
        "repo": task.repo.split("/")[-1] if task.repo else "",
        "repoFullName": task.repo or "",
        "branch": task.base_ref or "main",
        "model": task.model,
        "effort": meta.get("agent_effort"),
        "planMode": meta.get("plan_mode") is True,
        "source": "dashboard" if task.source == "web_ui" else task.source,
        "status": status,
        "viewed": is_viewed,
        "viewedAt": meta.get("last_viewed_at_ms"),
        "resolved": is_resolved,
        "createdAt": int(task.created_at.replace(tzinfo=UTC).timestamp() * 1000),
        "updatedAt": int(task.updated_at.replace(tzinfo=UTC).timestamp() * 1000),
        "messages": messages,
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


@router.get("/threads/{thread_id}/pr-diff")
async def get_thread_pr_diff(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    return {
        "prNumber": 1,
        "baseSha": "base",
        "headSha": "head",
        "truncated": False,
        "files": [],
    }


@router.get("/threads/{thread_id}/queued-messages")
async def get_thread_queued_messages(
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
    return {"tasks": [], "monitors": []}


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


@router.get("/skills")
async def list_skills(
    limit: int = 100,
    offset: int = 0,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del limit, offset, auth
    return {"items": [], "next_offset": None}


@router.get("/organization-skills")
async def list_organization_skills(
    limit: int = 100,
    cursor: str | None = None,
    auth: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    del limit, cursor, auth
    return {"items": [], "next_cursor": None}


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
