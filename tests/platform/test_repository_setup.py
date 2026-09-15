from unittest.mock import AsyncMock

import pytest

from alephat_platform.harness import worker
from alephat_platform.harness.sandboxes import ExecResult, SandboxRef


def test_repository_setup_command_clones_selected_repo_at_sandbox_root():
    command = worker._repository_setup_command(
        "acme/private-repo",
        "develop",
        "secret-token",
    )

    assert (
        "clone --branch develop --single-branch https://github.com/acme/private-repo.git ."
        in command
    )
    assert "secret-token" not in command
    assert "Sandbox workspace is not empty" in command
    assert "git rev-parse --show-toplevel" in command


@pytest.mark.parametrize("repo", ["missing-owner", "/missing-owner", "owner/"])
def test_repository_setup_command_rejects_invalid_repo(repo):
    with pytest.raises(ValueError, match="owner/name"):
        worker._repository_setup_command(repo, "main", None)


@pytest.mark.asyncio
async def test_prepare_task_repository_scopes_token_and_executes_clone(monkeypatch):
    token = AsyncMock(return_value="installation-token")
    monkeypatch.setattr(worker, "get_github_app_installation_token", token)

    class Provider:
        exec = AsyncMock(return_value=ExecResult(exit_code=0, stdout=""))

    provider = Provider()
    sandbox_ref = SandboxRef(provider="local", sandbox_id="sandbox")

    await worker._prepare_task_repository(
        provider,
        sandbox_ref,
        repo="acme/private-repo",
        base_ref="main",
    )

    token.assert_awaited_once_with(repositories=["private-repo"], log_errors=False)
    command = provider.exec.await_args.args[1]
    assert "https://github.com/acme/private-repo.git" in command
    assert provider.exec.await_args.kwargs["timeout"] == 240


@pytest.mark.asyncio
async def test_prepare_task_repository_fails_closed(monkeypatch):
    monkeypatch.setattr(
        worker,
        "get_github_app_installation_token",
        AsyncMock(return_value=None),
    )

    class Provider:
        exec = AsyncMock(
            return_value=ExecResult(exit_code=128, stdout="", stderr="repository not found")
        )

    with pytest.raises(RuntimeError, match="Could not prepare acme/private-repo"):
        await worker._prepare_task_repository(
            Provider(),
            SandboxRef(provider="local", sandbox_id="sandbox"),
            repo="acme/private-repo",
            base_ref="main",
        )
