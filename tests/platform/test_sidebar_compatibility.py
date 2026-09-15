from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from starlette.requests import Request

from alephat_platform.api.deps import AuthContext
from alephat_platform.api.routes_dashboard import (
    get_pinned_threads,
    get_thread_projects,
    get_threads_page,
    set_thread_pin,
)
from alephat_platform.common.models import Task


def task(repo: str | None, *, resolved: bool = False, source: str = "web_ui") -> Task:
    return Task(
        task_id=uuid4(),
        thread_id=str(uuid4()),
        title="Investigate failures",
        repo=repo,
        source=source,
        status="succeeded",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        metadata_={"resolved": resolved},
    )


def database(tasks: list[Task]) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = tasks
    db.execute.return_value = result
    return db


async def test_projects_group_repositories_and_respect_visibility_filters():
    auth = AuthContext(org_id=uuid4(), user_id=uuid4())
    latest = task("acme/app")
    older = task("ACME/APP")
    older.updated_at = latest.updated_at - timedelta(days=1)
    db = database(
        [
            older,
            task(None),
            task("acme/old", resolved=True),
            task("acme/cron", source="schedule"),
            latest,
        ]
    )

    projects = await get_thread_projects(db=db, auth=auth)
    assert [project["repoFullName"] for project in projects] == ["acme/app"]
    assert projects[0]["updatedAt"] == int(latest.updated_at.timestamp() * 1000)
    all_projects = await get_thread_projects(True, True, db, auth)
    assert {project["repoFullName"] for project in all_projects} == {
        "acme/app",
        "acme/old",
        "acme/cron",
    }


async def test_project_pages_filter_before_pagination_and_report_more_results():
    auth = AuthContext(org_id=uuid4(), user_id=uuid4())
    items = [
        task("acme/app"),
        task("acme/other"),
        task("acme/app"),
        task("acme/app", resolved=True),
    ]
    db = database(items)

    first = await get_threads_page(limit=1, repo="ACME/APP", resolved=False, db=db, auth=auth)
    second = await get_threads_page(
        limit=1, offset=1, repo="ACME/APP", resolved=False, db=db, auth=auth
    )
    assert first["items"] != second["items"]
    assert first["total"] == second["total"] == 2
    assert first["hasMore"] is True
    assert second["hasMore"] is False
    assert await get_threads_page(scope="automation", db=db, auth=auth) == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "hasMore": False,
    }


async def test_pins_are_persisted_per_user_and_unpin_preserves_other_users():
    auth = AuthContext(org_id=uuid4(), user_id=uuid4())
    other = str(uuid4())
    item = task("acme/app")
    item.metadata_ = {"pinned_by": [other]}
    db = database([item])
    assert await get_pinned_threads(db, auth) == []

    with patch(
        "alephat_platform.api.routes_dashboard._get_task_by_id_or_thread_id", return_value=item
    ):
        await set_thread_pin(item.thread_id, Request({"type": "http", "method": "POST"}), db, auth)
        assert len(await get_pinned_threads(db, auth)) == 1
        await set_thread_pin(
            item.thread_id, Request({"type": "http", "method": "DELETE"}), db, auth
        )
    assert await get_pinned_threads(db, auth) == []
    assert item.metadata_["pinned_by"] == [other]
