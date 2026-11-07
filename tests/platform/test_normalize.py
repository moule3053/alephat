import hashlib
import hmac
import time

from alephat_platform.common.enums import IngressCommand
from alephat_platform.webhook.normalize import (
    normalize_github,
    normalize_slack,
    verify_github_signature,
    verify_slack_signature,
)


def test_github_issue_opened():
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/pay"},
        "sender": {"login": "alice"},
        "issue": {
            "number": 7,
            "title": "Bug",
            "body": "fix it",
            "html_url": "https://github.com/acme/pay/issues/7",
        },
    }
    cmd = normalize_github("issues", payload, delivery_id="d1", org_id="org")
    assert cmd["command"] == IngressCommand.UPSERT_AND_ENQUEUE.value
    assert cmd["thread_id"] == "gh:acme/pay:issue:7"
    assert cmd["repo"] == "acme/pay"


def test_github_comment_without_mention_ignored():
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/pay"},
        "sender": {"login": "alice"},
        "issue": {"number": 1, "title": "x"},
        "comment": {"id": 9, "body": "just chatting"},
    }
    cmd = normalize_github("issue_comment", payload, delivery_id="d2", org_id="org")
    assert cmd["command"] == IngressCommand.IGNORE.value


def test_github_signature():
    secret = "s3cr3t"
    body = b'{"ok":true}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret, body, sig)
    assert not verify_github_signature(secret, body, "sha256=dead")


def test_slack_mention():
    payload = {
        "event_id": "Ev1",
        "event": {
            "type": "app_mention",
            "text": "<@U1> please fix repo:acme/pay",
            "channel": "C1",
            "ts": "1.2",
            "user": "U2",
        },
    }
    cmd = normalize_slack(payload, org_id="org", delivery_id="Ev1")
    assert cmd["command"] == IngressCommand.UPSERT_AND_ENQUEUE.value
    assert cmd["repo"] == "acme/pay"
    assert cmd["thread_id"] == "slack:C1:1.2"


def test_slack_channel_messages_require_a_mention_but_dms_do_not(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_USER_ID", "U1")
    event = {
        "type": "message",
        "text": "hello",
        "channel": "C1",
        "channel_type": "group",
        "ts": "1.2",
        "user": "U2",
    }
    assert normalize_slack({"event": event}, org_id="org", delivery_id="E1")["command"] == "ignore"
    event["text"] = "<@U1> hello"
    assert (
        normalize_slack({"event": event}, org_id="org", delivery_id="E2")["message"]["content"]
        == "hello"
    )
    event.update({"channel": "D1", "channel_type": "im", "text": "hello"})
    assert (
        normalize_slack({"event": event}, org_id="org", delivery_id="E3")["command"]
        == "upsert_and_enqueue"
    )


def test_slack_signature_rejects_replay():
    secret = "secret"
    body = b'{"event_id":"E1"}'
    timestamp = str(int(time.time()) - 301)
    signature = (
        "v0="
        + hmac.new(
            secret.encode(), f"v0:{timestamp}:{body.decode()}".encode(), hashlib.sha256
        ).hexdigest()
    )
    assert not verify_slack_signature(secret, body, timestamp, signature)


def test_slack_signature_accepts_current_request():
    secret = "secret"
    body = b'{"event_id":"E1"}'
    timestamp = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(
            secret.encode(), f"v0:{timestamp}:{body.decode()}".encode(), hashlib.sha256
        ).hexdigest()
    )
    assert verify_slack_signature(secret, body, timestamp, signature)
