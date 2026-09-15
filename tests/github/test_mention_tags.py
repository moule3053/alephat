"""Mention-handle matching, which keeps parallel deployments from double-firing."""

import importlib

import pytest

from agent.github import comments as github_comments


@pytest.fixture
def reload_tags(monkeypatch):
    def _reload(value: str | None):
        if value is None:
            monkeypatch.delenv("ALEPHAT_MENTION_TAGS", raising=False)
        else:
            monkeypatch.setenv("ALEPHAT_MENTION_TAGS", value)
        return importlib.reload(github_comments)

    yield _reload
    monkeypatch.delenv("ALEPHAT_MENTION_TAGS", raising=False)
    importlib.reload(github_comments)


@pytest.mark.parametrize(
    "body",
    [
        "@alephat please fix this",
        "hey @alephat, take a look",
        "@Alephat ping",
        "cc @alephat-dev",
        "@alephat: do the thing",
        "(@alephat)",
    ],
)
def test_matches_configured_handles(body: str) -> None:
    assert github_comments.mentions_alephat(body)


@pytest.mark.parametrize(
    "body",
    [
        "@alephat-preview please fix this",
        "@alephatfoo",
        "no mention here",
        "",
        None,
    ],
)
def test_ignores_longer_handles_and_empty(body: str | None) -> None:
    assert not github_comments.mentions_alephat(body)


def test_env_overrides_handles(reload_tags) -> None:
    module = reload_tags("@alephat-preview")
    assert module.ALEPHAT_TAGS == ("@alephat-preview",)
    assert module.mentions_alephat("@alephat-preview ship it")
    assert not module.mentions_alephat("@alephat ship it")


def test_blank_env_falls_back_to_defaults(reload_tags) -> None:
    module = reload_tags("  ,  ")
    assert module.ALEPHAT_TAGS == module._DEFAULT_ALEPHAT_TAGS
