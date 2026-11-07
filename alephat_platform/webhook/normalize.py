"""Normalize provider webhooks into ingress commands."""

import hashlib
import hmac
import re
import time
from typing import Any

from alephat_platform.common.enums import AgentType, IngressCommand, TaskSource


def verify_github_signature(secret: str | None, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True  # dev mode
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature_header)


def verify_slack_signature(
    secret: str | None,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
) -> bool:
    if not secret:
        return True
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    digest = hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def normalize_github(
    event: str,
    payload: dict[str, Any],
    *,
    delivery_id: str,
    org_id: str,
) -> dict[str, Any]:
    """Return ingress command envelope."""
    repo = (payload.get("repository") or {}).get("full_name")
    sender = (payload.get("sender") or {}).get("login")

    if event == "issues" and payload.get("action") in {"opened", "reopened"}:
        issue = payload.get("issue") or {}
        number = issue.get("number")
        body = issue.get("body") or ""
        title = issue.get("title") or f"Issue #{number}"
        thread_id = f"gh:{repo}:issue:{number}"
        return {
            "schema_version": 1,
            "command": IngressCommand.UPSERT_AND_ENQUEUE.value,
            "org_id": org_id,
            "source": TaskSource.GITHUB_ISSUE.value,
            "source_ref": f"{repo}#{number}",
            "thread_id": thread_id,
            "agent_type": AgentType.CODING.value,
            "actor": {"kind": "github_user", "id": sender},
            "repo": repo,
            "title": title,
            "message": {"kind": "user_input", "content": f"{title}\n\n{body}", "blocks": []},
            "idempotency_key": f"github:{delivery_id}",
            "metadata": {"issue_number": number, "html_url": issue.get("html_url")},
        }

    if event == "issue_comment" and payload.get("action") == "created":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        number = issue.get("number")
        body = comment.get("body") or ""
        # only react to @alephat or alephat mentions for PR/issue comments
        if not re.search(r"@?alephat\b", body, re.I):
            return _ignore(org_id, delivery_id, "no_mention")
        is_pr = "pull_request" in issue
        source = TaskSource.GITHUB_PR_COMMENT if is_pr else TaskSource.GITHUB_ISSUE
        thread_id = f"gh:{repo}:{'pr' if is_pr else 'issue'}:{number}"
        return {
            "schema_version": 1,
            "command": IngressCommand.UPSERT_AND_ENQUEUE.value,
            "org_id": org_id,
            "source": source.value,
            "source_ref": f"{repo}#{number}",
            "thread_id": thread_id,
            "agent_type": AgentType.CODING.value,
            "actor": {"kind": "github_user", "id": sender},
            "repo": repo,
            "title": issue.get("title"),
            "message": {"kind": "user_guidance", "content": body, "blocks": []},
            "idempotency_key": f"github:{delivery_id}",
            "metadata": {"issue_number": number, "comment_id": comment.get("id")},
        }

    if event in {"check_run", "check_suite", "workflow_run", "status"}:
        # CI failure autofix signal — policy gates applied by caller
        return {
            "schema_version": 1,
            "command": IngressCommand.CI_FAILURE.value,
            "org_id": org_id,
            "source": TaskSource.GITHUB_CI.value,
            "source_ref": repo or "unknown",
            "thread_id": f"gh:{repo}:ci:{delivery_id}",
            "agent_type": AgentType.CODING.value,
            "actor": {"kind": "github_user", "id": sender},
            "repo": repo,
            "title": f"CI event {event}",
            "message": {
                "kind": "system",
                "content": f"CI event {event}: {payload.get('action')}",
                "blocks": [],
            },
            "idempotency_key": f"github:{delivery_id}",
            "metadata": {"event": event},
        }

    return _ignore(org_id, delivery_id, f"unhandled_event:{event}")


def normalize_slack(
    payload: dict[str, Any],
    *,
    org_id: str,
    delivery_id: str,
) -> dict[str, Any]:
    import os

    if payload.get("type") == "url_verification":
        return {
            "schema_version": 1,
            "command": IngressCommand.IGNORE.value,
            "org_id": org_id,
            "challenge": payload.get("challenge"),
            "idempotency_key": f"slack:{delivery_id}",
        }

    event = payload.get("event") or {}
    event_type = event.get("type")
    if event_type not in {"app_mention", "message"}:
        return _ignore(org_id, delivery_id, "unhandled_slack")

    bot_user_id = os.environ.get("SLACK_BOT_USER_ID", "").strip()
    if (
        event.get("bot_id")
        or event.get("subtype")
        or (bot_user_id and event.get("user") == bot_user_id)
    ):
        return _ignore(org_id, delivery_id, "bot_or_subtype_message")

    text = event.get("text") or ""
    channel = event.get("channel")
    if (
        event_type == "message"
        and event.get("channel_type") != "im"
        and not (bot_user_id and f"<@{bot_user_id}>" in text)
    ):
        return _ignore(org_id, delivery_id, "no_mention")
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", text).strip()

    thread_ts = event.get("thread_ts") or event.get("ts")
    message_ts = event.get("ts") or thread_ts
    user = event.get("user")
    thread_id = f"slack:{channel}:{thread_ts}"
    # optional repo:owner/name
    repo = None
    m = re.search(r"repo:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    if m:
        repo = m.group(1)

    return {
        "schema_version": 1,
        "command": IngressCommand.UPSERT_AND_ENQUEUE.value,
        "org_id": org_id,
        "source": TaskSource.SLACK.value,
        "source_ref": f"{channel}/{thread_ts}",
        "thread_id": thread_id,
        "agent_type": AgentType.CODING.value,
        "actor": {"kind": "slack_user", "id": user},
        "repo": repo,
        "title": text[:120],
        "message": {"kind": "user_input", "content": text, "blocks": []},
        "idempotency_key": f"slack:{delivery_id}",
        "metadata": {
            "channel": channel,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
            "slack_team_id": payload.get("team_id"),
        },
    }


def _ignore(org_id: str, delivery_id: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": IngressCommand.IGNORE.value,
        "org_id": org_id,
        "idempotency_key": f"ignore:{delivery_id}",
        "metadata": {"reason": reason},
    }
