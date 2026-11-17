"""Temporal client, payload codec, and workflow/activity message contracts."""

import asyncio
import dataclasses
import logging
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
    WithStartWorkflowOperation,
)
from temporalio.common import (
    ActivityIDConflictPolicy,
    ActivityIDReusePolicy,
    RetryPolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.contrib.pydantic import PydanticPayloadConverter, ToJsonOptions
from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.service import RPCError, RPCStatusCode, TLSConfig

from alephat_platform.common.config import Settings, get_settings

logger = logging.getLogger(__name__)

TASK_WORKFLOW_NAME = "alephat.TaskAgentWorkflow"
SCHEDULE_WORKFLOW_NAME = "alephat.ScheduledTaskWorkflow"
SUBMIT_TURN_UPDATE = "submit_turn"
DECIDE_APPROVAL_UPDATE = "decide_approval"
CANCEL_CURRENT_UPDATE = "cancel_current"
GET_STATE_QUERY = "get_state"
STANDALONE_SLACK_ACK_ACTIVITY = "alephat.acknowledge_slack_message"
STREAMING_TOPIC = "agent-stream"

_COMPRESSION_ENCODING = b"binary/zlib"
_ENCRYPTION_ENCODING = b"binary/encrypted-fernet"
_ORIGINAL_ENCODING_HEADER = "original-encoding"
_INNER_ENCODING_HEADER = "inner-encoding"
_MIN_COMPRESS_BYTES = 1024


@dataclass
class TurnEnvelope:
    """One user or system turn submitted to a task workflow."""

    turn_id: str
    message_id: str
    content: str
    role: str = "user"
    message_ts: str | None = None
    run_id: str | None = None


@dataclass
class TurnAccepted:
    """Synchronous acknowledgement returned by ``submit_turn`` Update."""

    run_id: str
    mode: str


@dataclass
class ApprovalResolution:
    """Human decision delivered to a parked ``TaskAgentWorkflow``."""

    decision: str
    approval_id: str | None = None
    comment: str | None = None


@dataclass
class PendingApprovalView:
    """Read-only view of an active human-in-the-loop pause."""

    approval_id: str | None
    run_id: str
    kind: str
    prompt: str


@dataclass
class WorkflowStateSnapshot:
    """Live state exposed by ``TaskAgentWorkflow.get_state`` Query."""

    task_id: str
    active_run_id: str | None
    turn_in_progress: bool
    pending_turns: int
    pending_approval: PendingApprovalView | None
    sandbox_id: str | None
    transcript_count: int


@dataclass
class TaskWorkflowInput:
    """Input carried across ``TaskAgentWorkflow`` starts and Continue-As-New."""

    task_id: str
    org_id: str
    sandbox_id: str | None = None
    transcript: list[dict[str, object]] = field(default_factory=list)
    seen_turn_ids: list[str] = field(default_factory=list)
    pending_turns: list[TurnEnvelope] = field(default_factory=list)
    stream_state: WorkflowStreamState | None = None


@dataclass
class ScheduledAutomationInput:
    """Input to ``ScheduledTaskWorkflow`` triggered by a Temporal Schedule."""

    org_id: str
    user_id: str
    schedule_id: str


@dataclass
class SlackAckInput:
    """Input to the standalone ``alephat.acknowledge_slack_message`` Activity."""

    channel: str
    message_ts: str


class PlatformPayloadCodec(PayloadCodec):
    """Compresses large payloads and optionally encrypts Event History at rest."""

    def __init__(self, encryption_key: str | None = None) -> None:
        self._fernet = self._build_fernet(encryption_key)

    @staticmethod
    def _build_fernet(encryption_key: str | None) -> MultiFernet | None:
        if not encryption_key:
            return None
        fernets: list[Fernet] = []
        for raw_part in encryption_key.replace("\n", ",").split(","):
            part = raw_part.strip()
            if not part:
                continue
            padded = part + ("=" * ((4 - len(part) % 4) % 4))
            fernets.append(Fernet(padded.encode()))
        return MultiFernet(fernets) if fernets else None

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        encoded: list[Payload] = []
        for payload in payloads:
            current = payload
            raw = bytes(current.data)
            orig_enc = bytes(current.metadata.get("encoding", b""))
            if len(raw) >= _MIN_COMPRESS_BYTES and orig_enc != _COMPRESSION_ENCODING:
                compressed = zlib.compress(raw, level=6)
                if len(compressed) < len(raw):
                    current = Payload(
                        metadata={
                            "encoding": _COMPRESSION_ENCODING,
                            _ORIGINAL_ENCODING_HEADER: orig_enc,
                        },
                        data=compressed,
                    )
            if self._fernet is not None:
                inner_enc = bytes(current.metadata.get("encoding", b""))
                extra_meta = {
                    key: bytes(val) for key, val in current.metadata.items() if key != "encoding"
                }
                token = self._fernet.encrypt(bytes(current.data))
                current = Payload(
                    metadata={
                        **extra_meta,
                        "encoding": _ENCRYPTION_ENCODING,
                        _INNER_ENCODING_HEADER: inner_enc,
                    },
                    data=token,
                )
            encoded.append(current)
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        decoded: list[Payload] = []
        for payload in payloads:
            current = payload
            if bytes(current.metadata.get("encoding", b"")) == _ENCRYPTION_ENCODING:
                if self._fernet is None:
                    raise RuntimeError(
                        "TOKEN_ENCRYPTION_KEY is required to decode encrypted Temporal payloads"
                    )
                inner_enc = bytes(current.metadata.get(_INNER_ENCODING_HEADER, b""))
                restored_meta = {
                    key: bytes(val)
                    for key, val in current.metadata.items()
                    if key not in {"encoding", _INNER_ENCODING_HEADER}
                }
                restored_meta["encoding"] = inner_enc
                current = Payload(
                    metadata=restored_meta,
                    data=self._fernet.decrypt(bytes(current.data)),
                )
            if bytes(current.metadata.get("encoding", b"")) == _COMPRESSION_ENCODING:
                orig_enc = bytes(current.metadata.get(_ORIGINAL_ENCODING_HEADER, b""))
                current = Payload(
                    metadata={"encoding": orig_enc},
                    data=zlib.decompress(bytes(current.data)),
                )
            decoded.append(current)
        return decoded


class DataConverterFactory:
    """Construct matching DataConverters for API clients and DeepAgents workers."""

    def __init__(self, encryption_key: str | None = None) -> None:
        self._codec = PlatformPayloadCodec(encryption_key)

    def for_client(self) -> DataConverter:
        return DataConverter(
            payload_converter_class=self._payload_converter_class(),
            payload_codec=self._codec,
        )

    def for_worker(self) -> DataConverter:
        return dataclasses.replace(
            DataConverter.default,
            payload_codec=self._codec,
        )

    def _payload_converter_class(self) -> type[PydanticPayloadConverter]:
        class ExcludeUnsetPydanticConverter(PydanticPayloadConverter):
            def __init__(self) -> None:
                super().__init__(ToJsonOptions(exclude_unset=True))

        return ExcludeUnsetPydanticConverter


class TemporalTlsBuilder:
    """Build ``temporalio.service.TLSConfig`` from platform settings and mounted PEM files."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(self) -> TLSConfig | bool:
        has_cert_paths = bool(
            self._settings.temporal_tls_ca_cert_path or self._settings.temporal_tls_client_cert_path
        )
        if not self._settings.temporal_tls_enabled and not has_cert_paths:
            return False
        ca_bytes = (
            Path(self._settings.temporal_tls_ca_cert_path).read_bytes()
            if self._settings.temporal_tls_ca_cert_path
            else None
        )
        cert_bytes = (
            Path(self._settings.temporal_tls_client_cert_path).read_bytes()
            if self._settings.temporal_tls_client_cert_path
            else None
        )
        key_bytes = (
            Path(self._settings.temporal_tls_client_key_path).read_bytes()
            if self._settings.temporal_tls_client_key_path
            else None
        )
        if ca_bytes is None and cert_bytes is None and not self._settings.temporal_tls_server_name:
            return True
        return TLSConfig(
            server_root_ca_cert=ca_bytes,
            client_cert=cert_bytes,
            client_private_key=key_bytes,
            domain=self._settings.temporal_tls_server_name,
        )


class TemporalPlatformClient:
    """Stateful Temporal client wrapper shared by API and webhook services."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: Client | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.address = cfg.temporal_address
        self.namespace = cfg.temporal_namespace
        self.task_queue = cfg.temporal_task_queue
        self._encryption_key = cfg.token_encryption_key
        self._tls_builder = TemporalTlsBuilder(cfg)
        self._client = client
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None

    @classmethod
    def from_request(cls, request: object) -> TemporalPlatformClient:
        app_obj = getattr(request, "app", None)
        state_obj = getattr(app_obj, "state", None) if app_obj is not None else None
        existing = getattr(state_obj, "temporal", None) if state_obj is not None else None
        if isinstance(existing, cls):
            return existing
        return cls()

    def workflow_id(self, task_id: uuid.UUID | str) -> str:
        return f"alephat-task:{task_id}"

    def schedule_key(self, schedule_id: str) -> str:
        return f"alephat-schedule:{schedule_id}"

    async def connect(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                converter = DataConverterFactory(self._encryption_key).for_client()
                self._client = await Client.connect(
                    self.address,
                    namespace=self.namespace,
                    data_converter=converter,
                    tls=self._tls_builder.build(),
                )
            return self._client

    async def supervisor_loop(self, stop_event: asyncio.Event) -> None:
        """Maintain a warm Temporal connection across transient startup/network restarts."""
        while not stop_event.is_set():
            if self._client is None:
                try:
                    await self.connect()
                    logger.info(
                        "Connected to Temporal service",
                        extra={
                            "temporal_address": self.address,
                            "temporal_namespace": self.namespace,
                        },
                    )
                except Exception:
                    logger.warning(
                        "Temporal service not yet reachable; retrying",
                        extra={"temporal_address": self.address},
                        exc_info=True,
                    )
            else:
                healthy = await self.check_health()
                if not healthy:
                    self._client = None
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                continue

    async def check_health(self) -> bool:
        try:
            client = await self.connect()
            return await client.service_client.check_health()
        except Exception:
            logger.warning(
                "Temporal health check failed",
                extra={"temporal_address": self.address},
                exc_info=True,
            )
            return False

    async def submit_turn(
        self,
        *,
        task_id: uuid.UUID,
        org_id: uuid.UUID,
        turn: TurnEnvelope,
    ) -> TurnAccepted:
        client = await self.connect()
        wf_id = self.workflow_id(task_id)
        start_op = WithStartWorkflowOperation(
            TASK_WORKFLOW_NAME,
            TaskWorkflowInput(task_id=str(task_id), org_id=str(org_id)),
            id=wf_id,
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        return await client.execute_update_with_start_workflow(
            SUBMIT_TURN_UPDATE,
            turn,
            start_workflow_operation=start_op,
            id=turn.turn_id,
            result_type=TurnAccepted,
        )

    async def submit_approval(
        self,
        *,
        task_id: uuid.UUID,
        resolution: ApprovalResolution,
    ) -> bool:
        client = await self.connect()
        handle = client.get_workflow_handle(self.workflow_id(task_id))
        update_id = (
            f"approval:{resolution.approval_id}:{resolution.decision}"
            if resolution.approval_id
            else None
        )
        return await handle.execute_update(
            DECIDE_APPROVAL_UPDATE,
            resolution,
            id=update_id,
            result_type=bool,
        )

    async def cancel_task(self, *, task_id: uuid.UUID, reason: str) -> None:
        client = await self.connect()
        handle = client.get_workflow_handle(self.workflow_id(task_id))
        try:
            await handle.execute_update(
                CANCEL_CURRENT_UPDATE,
                reason,
                result_type=bool,
            )
        except RPCError as exc:
            if exc.status in {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}:
                return
            raise

    async def terminate_task(self, *, task_id: uuid.UUID, reason: str) -> None:
        client = await self.connect()
        handle = client.get_workflow_handle(self.workflow_id(task_id))
        try:
            await handle.terminate(reason=reason)
        except RPCError as exc:
            if exc.status in {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}:
                return
            raise

    async def acknowledge_slack_message(self, *, channel: str, message_ts: str) -> None:
        """Enqueue a durable Temporal Standalone Activity to add an eyes reaction."""
        client = await self.connect()
        await client.start_activity(
            STANDALONE_SLACK_ACK_ACTIVITY,
            SlackAckInput(channel=channel, message_ts=message_ts),
            id=f"slack-ack:{channel}:{message_ts}",
            task_queue=self.task_queue,
            start_to_close_timeout=timedelta(seconds=15),
            id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=ActivityIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def sync_schedule(
        self,
        schedule_id: str,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        cron_expression: str | None,
        enabled: bool,
    ) -> str | None:
        """Create, update, or remove a native Temporal Schedule for an automation."""
        if not cron_expression or not cron_expression.strip():
            await self.delete_schedule(schedule_id)
            return None
        client = await self.connect()
        key = self.schedule_key(schedule_id)
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                SCHEDULE_WORKFLOW_NAME,
                ScheduledAutomationInput(
                    org_id=str(org_id),
                    user_id=str(user_id),
                    schedule_id=schedule_id,
                ),
                id=f"alephat-scheduled-run:{schedule_id}",
                task_queue=self.task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=[cron_expression.strip()]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE),
            state=ScheduleState(paused=not enabled),
        )
        try:
            await client.create_schedule(key, schedule)
        except ScheduleAlreadyRunningError:
            handle = client.get_schedule_handle(key)

            def apply_update(_input: ScheduleUpdateInput) -> ScheduleUpdate:
                return ScheduleUpdate(schedule=schedule)

            await handle.update(apply_update)
        return key

    async def delete_schedule(self, schedule_id: str) -> None:
        client = await self.connect()
        handle = client.get_schedule_handle(self.schedule_key(schedule_id))
        try:
            await handle.delete()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            raise
