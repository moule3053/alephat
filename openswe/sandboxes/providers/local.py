import os
import shutil
from pathlib import Path

from deepagents.backends import LocalShellBackend

from openswe.config import ENV

SANDBOX_GITCONFIG = ".gitconfig-sandbox"
LOCAL_SHELL_ENV_EXCLUDE = {
    "ANTHROPIC_API_KEY",
    "BASETEN_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "LANGSMITH_API_KEY",
    "ALEPHAT_OPENAI_OAUTH_ACCOUNT_FILE",
    "ALEPHAT_OPENAI_OAUTH_BROKER_TOKEN",
    "ALEPHAT_OPENAI_OAUTH_BROKER_URL",
    "OPENAI_API_KEY",
}


def _scoped_git_config_env(root_dir: str) -> dict[str, str]:
    """Point `git config --global` at a sandbox-local file outside the repo tree.

    Local sandboxes run on the host, so the bot identity every run writes would
    otherwise overwrite the developer's own `~/.gitconfig` user.name/email. The
    scoped file includes the real one so credential helpers and aliases survive,
    and lives next to `root_dir` so `root_dir` stays empty for `git clone .`.
    """
    resolved = Path(root_dir).resolve()
    scoped = resolved.parent / f"{SANDBOX_GITCONFIG}-{resolved.name}"
    if not scoped.exists():
        host = Path.home() / ".gitconfig"
        scoped.write_text(f"[include]\n\tpath = {host}\n" if host.exists() else "")
    return {"GIT_CONFIG_GLOBAL": str(scoped)}


def _scoped_gh_wrapper_env(root_dir: str, base_path: str) -> dict[str, str]:
    real_gh = shutil.which("gh", path=base_path)
    if not real_gh:
        return {}
    resolved = Path(root_dir).resolve()
    bin_dir = resolved.parent / f".bin-{resolved.name}"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "gh"
    if not wrapper.exists():
        wrapper.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ -z "${GH_TOKEN:-}" ]; then\n'
            '  auth_header="$(git config --get http.https://github.com/.extraheader || true)"\n'
            '  case "$auth_header" in\n'
            '    "Authorization: Basic "*)\n'
            '      encoded="${auth_header#Authorization: Basic }"\n'
            '      credentials="$(printf \'%s\' "$encoded" | base64 -d 2>/dev/null || printf \'%s\' "$encoded" | base64 -D 2>/dev/null || true)"\n'
            '      GH_TOKEN="${credentials#x-access-token:}"\n'
            "      export GH_TOKEN\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            f'exec "{real_gh}" "$@"\n'
        )
        wrapper.chmod(0o755)
    return {"PATH": f"{bin_dir}:{base_path}"}


def create_local_sandbox(
    sandbox_id: str | None = None, *, root_dir: str | None = None
) -> LocalShellBackend:
    """Create a local shell sandbox with no isolation.

    WARNING: This runs commands directly on the host machine with no sandboxing.
    Only use for local development with human-in-the-loop enabled.
    """
    root_dir = root_dir or ENV.LOCAL_SANDBOX_ROOT_DIR.get(os.getcwd())
    os.makedirs(root_dir, exist_ok=True)

    env = {key: value for key, value in os.environ.items() if key not in LOCAL_SHELL_ENV_EXCLUDE}
    if not ENV.GIT_CONFIG_GLOBAL.optional():
        env.update(_scoped_git_config_env(root_dir))
    env.update(_scoped_gh_wrapper_env(root_dir, env.get("PATH", os.defpath)))

    return LocalShellBackend(
        root_dir=root_dir,
        virtual_mode=True,
        inherit_env=False,
        env=env,
    )
