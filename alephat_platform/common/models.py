"""SQLAlchemy ORM models for the platform schema."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Org(Base):
    __tablename__ = "orgs"
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrgMembership(Base):
    __tablename__ = "org_memberships"

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role: Mapped[str] = mapped_column(Text, default="member")


class OrgSettings(Base):
    __tablename__ = "org_settings"
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    default_model: Mapped[str] = mapped_column(Text, default="gpt-4.1")
    default_sandbox_provider: Mapped[str] = mapped_column(Text, default="agent_sandbox")
    enabled_sandbox_providers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY['daytona','agent_sandbox','opensandbox']")
    )
    mcp_stdio_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    max_mcp_servers_per_run: Mapped[int] = mapped_column(Integer, default=10)
    sandbox_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    preferred_model: Mapped[str | None] = mapped_column(Text)
    preferred_sandbox_provider: Mapped[str | None] = mapped_column(Text)
    default_mcp_server_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("ARRAY[]::uuid[]")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class McpServer(Base):
    __tablename__ = "mcp_servers"
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(Text)
    args: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    auth_ciphertext: Mapped[str | None] = mapped_column(Text)
    headers_ciphertext: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    tool_denylist: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    agent_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    execution: Mapped[str] = mapped_column(Text, default="harness")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY[]::text[]")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("org_id", "thread_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(Text, default="coding")
    status: Mapped[str] = mapped_column(Text, default="queued")
    title: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(Text)
    base_ref: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    sandbox_provider: Mapped[str] = mapped_column(Text, default="agent_sandbox")
    mcp_server_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("ARRAY[]::uuid[]")
    )
    mcp_snapshot: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    park_reason: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    worker_id: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, default="user")
    kind: Mapped[str] = mapped_column(Text, default="user_input")
    content: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    visibility: Mapped[str] = mapped_column(Text, default="all")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    seq: Mapped[int] = mapped_column(BigInteger, Identity())


class Approval(Base):
    __tablename__ = "approvals"
    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, default="plan_approval")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunEvent(Base):
    __tablename__ = "run_events"
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity())
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SandboxRow(Base):
    __tablename__ = "sandboxes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    sandbox_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="active")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphCheckpoint(Base):
    __tablename__ = "graph_checkpoints"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    thread_key: Mapped[str] = mapped_column(Text, default="default")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    blob: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    msg_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AllowedSlackBot(Base):
    __tablename__ = "allowed_slack_bots"
    __table_args__ = (UniqueConstraint("org_id", "team_id", "bot_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    team_id: Mapped[str] = mapped_column(Text, nullable=False)
    bot_id: Mapped[str] = mapped_column(Text, nullable=False)
    slack_user_id: Mapped[str] = mapped_column(Text, default="")
    app_id: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommandIdempotency(Base):
    __tablename__ = "command_idempotency"
    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    route: Mapped[str] = mapped_column(Text, primary_key=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
