"""Environment configuration for platform services."""

import os
import socket
from dataclasses import dataclass, field
from functools import lru_cache


class EnvReader:
    """Typed environment reader for platform settings."""

    def get(self, name: str, default: str | None = None) -> str | None:
        value = os.environ.get(name)
        if value is None or value == "":
            return default
        return value

    def get_int(self, name: str, default: int) -> int:
        raw = self.get(name)
        if raw is None:
            return default
        return int(raw)

    def get_bool(self, name: str, default: bool = False) -> bool:
        raw = self.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def database_url(self) -> str:
        configured = self.get("DATABASE_URL")
        if configured:
            return configured
        host = self.get("DATABASE_HOST", "localhost") or "localhost"
        port = self.get("DATABASE_PORT", "5432") or "5432"
        name = self.get("DATABASE_NAME", "postgres") or "postgres"
        user = self.get("DATABASE_USER", "postgres") or "postgres"
        password = self.get("DATABASE_PASSWORD")
        auth = f"{user}:{password}@" if password else f"{user}@"
        return f"postgresql+asyncpg://{auth}{host}:{port}/{name}"


_ENV = EnvReader()


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=_ENV.database_url)
    temporal_address: str = field(
        default_factory=lambda: _ENV.get("TEMPORAL_ADDRESS", "localhost:7233") or "localhost:7233"
    )
    temporal_namespace: str = field(
        default_factory=lambda: _ENV.get("TEMPORAL_NAMESPACE", "default") or "default"
    )
    temporal_task_queue: str = field(
        default_factory=lambda: (
            _ENV.get("TEMPORAL_TASK_QUEUE", "alephat-agents") or "alephat-agents"
        )
    )
    temporal_workflow_idle_seconds: int = field(
        default_factory=lambda: _ENV.get_int("TEMPORAL_WORKFLOW_IDLE_SECONDS", 3600)
    )
    temporal_tls_enabled: bool = field(default_factory=lambda: _ENV.get_bool("TEMPORAL_TLS", False))
    temporal_tls_ca_cert_path: str | None = field(
        default_factory=lambda: _ENV.get("TEMPORAL_TLS_CA_CERT_PATH")
    )
    temporal_tls_client_cert_path: str | None = field(
        default_factory=lambda: _ENV.get("TEMPORAL_TLS_CLIENT_CERT_PATH")
    )
    temporal_tls_client_key_path: str | None = field(
        default_factory=lambda: _ENV.get("TEMPORAL_TLS_CLIENT_KEY_PATH")
    )
    temporal_tls_server_name: str | None = field(
        default_factory=lambda: _ENV.get("TEMPORAL_TLS_SERVER_NAME")
    )
    token_encryption_key: str | None = field(
        default_factory=lambda: _ENV.get("TOKEN_ENCRYPTION_KEY")
    )
    llm_mode: str = field(default_factory=lambda: _ENV.get("LLM_MODE", "auto") or "auto")
    litellm_enabled: bool = field(default_factory=lambda: _ENV.get_bool("LITELLM_ENABLED", False))
    litellm_base_url: str = field(default_factory=lambda: _ENV.get("LITELLM_BASE_URL", "") or "")
    litellm_api_key: str = field(
        default_factory=lambda: _ENV.get("LITELLM_API_KEY", "sk-litellm") or ""
    )
    default_llm_provider: str = field(
        default_factory=lambda: _ENV.get("DEFAULT_LLM_PROVIDER", "openai") or "openai"
    )
    openai_api_key: str | None = field(default_factory=lambda: _ENV.get("OPENAI_API_KEY"))
    openai_base_url: str = field(
        default_factory=lambda: (
            _ENV.get("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1"
        )
    )
    anthropic_api_key: str | None = field(default_factory=lambda: _ENV.get("ANTHROPIC_API_KEY"))
    anthropic_base_url: str = field(
        default_factory=lambda: (
            _ENV.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            or "https://api.anthropic.com"
        )
    )
    fireworks_api_key: str | None = field(default_factory=lambda: _ENV.get("FIREWORKS_API_KEY"))
    fireworks_base_url: str = field(
        default_factory=lambda: (
            _ENV.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
            or "https://api.fireworks.ai/inference/v1"
        )
    )
    google_api_key: str | None = field(
        default_factory=lambda: _ENV.get("GOOGLE_API_KEY") or _ENV.get("GEMINI_API_KEY")
    )
    default_model: str = field(
        default_factory=lambda: (
            _ENV.get("DEFAULT_MODEL", "openai:gpt-4o-mini") or "openai:gpt-4o-mini"
        )
    )
    enabled_models: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            model.strip()
            for model in (_ENV.get("ENABLED_MODELS", "") or "").split(",")
            if model.strip()
        )
    )
    default_sandbox_provider: str = field(
        default_factory=lambda: (
            _ENV.get("DEFAULT_SANDBOX_PROVIDER", "agent_sandbox") or "agent_sandbox"
        )
    )
    bootstrap_dev_defaults: bool = field(
        default_factory=lambda: _ENV.get_bool("BOOTSTRAP_DEV_DEFAULTS", False)
    )
    max_run_duration_seconds: int = field(
        default_factory=lambda: _ENV.get_int("MAX_RUN_DURATION_SECONDS", 4 * 3600)
    )
    agent_recursion_limit: int = field(
        default_factory=lambda: _ENV.get_int("AGENT_RECURSION_LIMIT", 1000)
    )
    max_run_retries: int = field(default_factory=lambda: _ENV.get_int("MAX_RUN_RETRIES", 3))
    api_auth_disabled: bool = field(
        default_factory=lambda: _ENV.get_bool("API_AUTH_DISABLED", True)
    )
    platform_api_token: str | None = field(default_factory=lambda: _ENV.get("PLATFORM_API_TOKEN"))
    platform_jwt_secret: str | None = field(default_factory=lambda: _ENV.get("PLATFORM_JWT_SECRET"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in (_ENV.get("CORS_ORIGINS", "http://localhost:3000") or "").split(",")
            if origin.strip()
        )
    )
    default_org_id: str = field(
        default_factory=lambda: (
            _ENV.get("DEFAULT_ORG_ID", "00000000-0000-4000-8000-000000000001")
            or "00000000-0000-4000-8000-000000000001"
        )
    )
    default_user_id: str = field(
        default_factory=lambda: (
            _ENV.get("DEFAULT_USER_ID", "00000000-0000-4000-8000-000000000002")
            or "00000000-0000-4000-8000-000000000002"
        )
    )
    github_webhook_secret: str | None = field(
        default_factory=lambda: _ENV.get("GITHUB_WEBHOOK_SECRET")
    )
    slack_signing_secret: str | None = field(
        default_factory=lambda: _ENV.get("SLACK_SIGNING_SECRET")
    )
    slack_app_token: str | None = field(default_factory=lambda: _ENV.get("SLACK_APP_TOKEN"))
    slack_bot_token: str = field(default_factory=lambda: _ENV.get("SLACK_BOT_TOKEN", "") or "")
    slack_renderer_url: str = field(
        default_factory=lambda: _ENV.get("SLACK_RENDERER_URL", "http://127.0.0.1:8082") or ""
    )
    slack_renderer_token: str = field(
        default_factory=lambda: _ENV.get("SLACK_RENDERER_TOKEN", "") or ""
    )
    slack_user_map: str = field(default_factory=lambda: _ENV.get("SLACK_USER_MAP", "{}") or "{}")
    webhook_signatures_required: bool = field(
        default_factory=lambda: _ENV.get_bool("WEBHOOK_SIGNATURES_REQUIRED", False)
    )
    service_name: str = field(
        default_factory=lambda: _ENV.get("SERVICE_NAME", "platform") or "platform"
    )
    worker_id: str = field(
        default_factory=lambda: (
            _ENV.get("WORKER_ID") or socket.gethostname() or f"worker-{os.getpid()}"
        )
    )
    daytona_api_key: str | None = field(default_factory=lambda: _ENV.get("DAYTONA_API_KEY"))
    allow_stub_sandboxes: bool = field(
        default_factory=lambda: _ENV.get_bool("ALLOW_STUB_SANDBOXES", False)
    )
    agent_sandbox_kubeconfig: str | None = field(
        default_factory=lambda: _ENV.get("AGENT_SANDBOX_KUBECONFIG")
    )
    agent_sandbox_namespace: str = field(
        default_factory=lambda: (
            _ENV.get("AGENT_SANDBOX_NAMESPACE", "agent-sandboxes") or "agent-sandboxes"
        )
    )
    agent_sandbox_template: str = field(
        default_factory=lambda: (
            _ENV.get("AGENT_SANDBOX_TEMPLATE", "python-sandbox-warmpool")
            or "python-sandbox-warmpool"
        )
    )
    agent_sandbox_api_timeout_seconds: int = field(
        default_factory=lambda: _ENV.get_int("AGENT_SANDBOX_API_TIMEOUT_SECONDS", 30)
    )
    opensandbox_base_url: str | None = field(
        default_factory=lambda: _ENV.get("OPENSANDBOX_BASE_URL")
    )
    opensandbox_api_key: str | None = field(default_factory=lambda: _ENV.get("OPENSANDBOX_API_KEY"))
    mcp_stdio_allowed: bool = field(
        default_factory=lambda: _ENV.get_bool("MCP_STDIO_ALLOWED", False)
    )
    mcp_allow_private_networks: bool = field(
        default_factory=lambda: _ENV.get_bool("MCP_ALLOW_PRIVATE_NETWORKS", False)
    )
    mcp_tool_timeout_seconds: int = field(
        default_factory=lambda: _ENV.get_int("MCP_TOOL_TIMEOUT_SECONDS", 120)
    )
    max_mcp_servers_per_run: int = field(
        default_factory=lambda: _ENV.get_int("MAX_MCP_SERVERS_PER_RUN", 10)
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
