"""Pure serializable activity contracts shared between Workflows and Workers."""

from dataclasses import dataclass, field

PREPARE_TURN_ACTIVITY = "alephat.prepare_turn"
RECORD_STEP_ACTIVITY = "alephat.record_step"
ENTER_PARKED_ACTIVITY = "alephat.enter_parked_state"
EXIT_PARKED_ACTIVITY = "alephat.exit_parked_state"
COMPLETE_TURN_ACTIVITY = "alephat.complete_turn"
INVOKE_MCP_TOOL_ACTIVITY = "alephat.invoke_mcp_tool"
RENDER_SLACK_CARD_ACTIVITY = "alephat.render_slack_card"
UPLOAD_SLACK_FILE_ACTIVITY = "alephat.upload_slack_file"
TRIGGER_SCHEDULED_AUTOMATION_ACTIVITY = "alephat.trigger_scheduled_automation"


@dataclass
class McpToolWireSpec:
    """JSON-safe MCP tool definition passed from prepare_turn into the Workflow."""

    namespaced_name: str
    server_id: str
    server_name: str
    tool_name: str
    description: str
    args_schema: dict[str, object]
    server_config: dict[str, object]


@dataclass
class PrepareTurnInput:
    task_id: str
    run_id: str
    temporal_run_id: str
    sandbox_id: str | None = None
    load_history: bool = False
    message_ts: str | None = None


@dataclass
class PreparedTurn:
    task_id: str
    run_id: str
    agent_type: str
    repo: str | None
    base_ref: str | None
    model: str
    effort: str | None
    sandbox_provider: str
    sandbox_id: str
    sandbox_is_stub: bool
    workspace_path: str | None
    metadata: dict[str, object] = field(default_factory=dict)
    slack_enabled: bool = False
    slack_instructions: str = ""
    mcp_tools: list[McpToolWireSpec] = field(default_factory=list)
    prior_messages: list[dict[str, object]] = field(default_factory=list)
    recursion_limit: int = 1000


@dataclass
class RecordStepInput:
    task_id: str
    run_id: str
    event_type: str
    payload: dict[str, object] = field(default_factory=dict)
    messages: list[dict[str, object]] = field(default_factory=list)
    preview_text: str | None = None


@dataclass
class EnterParkedInput:
    task_id: str
    run_id: str
    reason: str
    prompt: str
    approval_payload: dict[str, object] = field(default_factory=dict)
    messages: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ExitParkedInput:
    task_id: str
    run_id: str


@dataclass
class CompleteTurnInput:
    task_id: str
    run_id: str
    success: bool
    cancelled: bool = False
    final_message: str = ""
    error_code: str | None = None
    sandbox_provider: str = "local"
    sandbox_id: str | None = None
    repo: str | None = None
    messages: list[dict[str, object]] = field(default_factory=list)


@dataclass
class InvokeMcpToolInput:
    task_id: str
    run_id: str
    namespaced_name: str
    server_id: str
    tool_name: str
    server_config: dict[str, object]
    args: dict[str, object] = field(default_factory=dict)


@dataclass
class RenderSlackCardInput:
    task_id: str
    run_id: str
    spec_json: str
    fallback_text: str


@dataclass
class UploadSlackFileInput:
    task_id: str
    run_id: str
    sandbox_provider: str
    sandbox_id: str
    path: str
    title: str
