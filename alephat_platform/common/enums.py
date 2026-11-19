"""Domain enums aligned with docs/SERVICE_CONTRACTS.md."""

from enum import StrEnum


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARKED = "parked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    ACTIVE = "active"
    PARKED = "parked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ParkReason(StrEnum):
    PLAN_APPROVAL = "plan_approval"
    AGENT_QUESTION = "agent_question"
    USER_GUIDANCE_REQUIRED = "user_guidance_required"
    EXTERNAL_INPUT_REQUIRED = "external_input_required"


class TaskSource(StrEnum):
    GITHUB_ISSUE = "github_issue"
    GITHUB_PR_COMMENT = "github_pr_comment"
    GITHUB_CI = "github_ci"
    SLACK = "slack"
    API = "api"
    WEB_UI = "web_ui"


class AgentType(StrEnum):
    CODING = "coding"
    REVIEWER = "reviewer"
    ANALYZER = "analyzer"
    CHAT = "chat"


class SandboxProvider(StrEnum):
    DAYTONA = "daytona"
    AGENT_SANDBOX = "agent_sandbox"
    SUBSTRATE = "substrate"
    OPENSANDBOX = "opensandbox"
    LOCAL = "local"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class McpScope(StrEnum):
    USER = "user"
    ORG = "org"


class McpTransport(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class McpExecution(StrEnum):
    HARNESS = "harness"
    SANDBOX = "sandbox"


class McpMode(StrEnum):
    INHERIT = "inherit"
    REPLACE = "replace"
    APPEND = "append"


class MessageKind(StrEnum):
    USER_INPUT = "user_input"
    USER_GUIDANCE = "user_guidance"
    USER_ANSWER = "user_answer"
    SYSTEM = "system"
    ASSISTANT = "assistant"


class IngressCommand(StrEnum):
    UPSERT_AND_ENQUEUE = "upsert_and_enqueue"
    APPEND_MESSAGE = "append_message"
    REQUEST_CANCEL = "request_cancel"
    CI_FAILURE = "ci_failure"
    IGNORE = "ignore"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)

DEFAULT_SANDBOX_PROVIDER = SandboxProvider.AGENT_SANDBOX
SCHEMA_VERSION = 1
