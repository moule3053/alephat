# Multi-service platform — service contracts

Target architecture for a k8s-native, long-running remote coding agent platform.

**Deployables:** `ui` · `api` · `webhook` · `harness` **Platform deps:** PostgreSQL · NATS JetStream · sandbox providers (**Daytona** default, **agent-sandbox**, **OpenSandbox**) · **LLM** via direct providers and/or optional **LiteLLM**

This document is the **contract** between services: identities, state machine, HTTP APIs, NATS subjects, lease/idempotency rules, model/sandbox/MCP contracts, and event schemas.
Implementation may evolve; breaking contract changes require a version bump (`api` and message `schema_version`).

**Implementation:** Python package `alephat_platform/` (see [`PLATFORM.md`](./PLATFORM.md)), schema `migrations/001_init.sql`, deploy under `deploy/`.

---

## 1. Design invariants

1. **Postgres is source of truth.**
   NATS is delivery only.
1. **Harness persists and checkpoints only to Postgres** — graph/agent state, run progress, and recovery points must be durable in Postgres before ack boundaries that allow another worker to take over (lease loss, park, process exit).
   No reliance on local disk or in-memory-only state for resume.
1. **At most one active harness** may hold the lease for a given `task_id` at a time.
1. **One harness slot = one run attempt** (pod concurrency 1 initially).
1. **Human wait parks the run** and releases the worker; compute wait holds the worker.
1. **Transactional outbox:** producers write domain rows + outbox in one DB transaction; a publisher relays outbox → JetStream.
1. **Idempotent consumers:** every external delivery and every NATS message has a stable idempotency key stored in Postgres.
1. **Harness is extensible by `agent_type`**, not by new deployables (until scale profiles force a split).
1. **LLM access is pluggable** — harness uses **direct provider APIs** (OpenAI, Anthropic, Fireworks, Google) by default, or an optional **LiteLLM** proxy when enabled (`LLM_MODE` / `LITELLM_ENABLED`).
   Provider credentials stay on the harness; never in the sandbox.
1. **Sandbox provider is pluggable and user-selectable** — supported: `agent_sandbox` (default), `daytona`, `opensandbox`.
   Selection resolves per task (§10).
1. **Users and orgs can configure MCP servers** — harness loads enabled MCP configs for a run and exposes their tools to the agent.
   MCP traffic runs **on the harness (server-side)**, not inside the sandbox, unless a server is explicitly marked sandbox-side (§11).

---

## 2. Core identities

| ID | Format | Meaning |
|---|---|---|
| `org_id` | UUID | Tenant / workspace |
| `task_id` | UUID | Logical unit of work (one coding goal / thread of work) |
| `run_id` | UUID | One execution attempt of a task (retries get a new `run_id`) |
| `thread_id` | string | Stable correlation with external surface (Slack thread, GH issue, UI session). Unique per `(org_id, source, source_ref)` |
| `message_id` | UUID | User/system/assistant message on a task |
| `approval_id` | UUID | Plan (or other) approval request |
| `event_id` | UUID | Append-only run event for UI/SSE |
| `sandbox_id` | string | Provider-native sandbox identifier |
| `sandbox_provider` | enum | `daytona` \| `agent_sandbox` \| `opensandbox` |
| `model` | string | Model id: `openai:gpt-4o-mini`, `anthropic:claude-…`, or LiteLLM alias when gateway is used |
| `mcp_server_id` | UUID | Configured MCP server record |
| `idempotency_key` | string | Dedup key for webhooks and commands |

### External source reference

```text
source ∈ { github_issue, github_pr_comment, github_ci, slack, api, web_ui }
source_ref = provider-native locator (e.g. "octo/repo#123", "T01/C02/1739.0001")
```

`thread_id` derivation must be **deterministic** so follow-ups route to the same task.

---

## 3. Run state machine

### 3.1 Task status (long-lived)

A **task** is the durable work item users care about.

```text
                 ┌──────────────────────────────────────────────┐
                 │                                              │
                 ▼                                              │
              queued ──────► running ──────► parked ────────────┤
                 │              │    ▲          │               │
                 │              │    └──────────┘               │
                 │              │         (resume)              │
                 │              ▼                               │
                 ├────────► succeeded                          │
                 ├────────► failed                             │
                 └────────► cancelled ◄────────────────────────┘
```

| Status | Meaning | Worker held? |
|---|---|---|
| `queued` | Runnable; waiting for harness claim | No |
| `running` | Lease held; agent loop active | Yes |
| `parked` | Waiting on human (plan, question, guidance gate) | No |
| `succeeded` | Terminal success | No |
| `failed` | Terminal failure (exhausted retries or hard error) | No |
| `cancelled` | Terminal cancel requested and honored | No |

### 3.2 Run status (attempt)

Each claim creates or continues a **run**:

| Status | Meaning |
|---|---|
| `pending` | Created, not yet leased |
| `leased` | Worker claimed; starting |
| `active` | Agent loop running |
| `parked` | Checkpointed; waiting human |
| `succeeded` | Attempt finished OK |
| `failed` | Attempt failed |
| `cancelled` | Attempt cancelled |
| `superseded` | Replaced by a newer run (rare; admin redrive) |

### 3.3 Legal transitions

| From | To | Trigger |
|---|---|---|
| `queued` | `running` | Harness acquires lease |
| `running` | `parked` | Agent requests approval / question; harness checkpoints + releases |
| `running` | `succeeded` | Agent completes |
| `running` | `failed` | Unrecoverable error or retry budget exhausted |
| `running` | `cancelled` | Cancel observed; graceful shutdown |
| `parked` | `queued` | Human responds / approve / reject with continue / new guidance |
| `parked` | `cancelled` | Cancel while parked |
| `queued` | `cancelled` | Cancel before claim |
| `*` terminal | — | No further transitions |

**Reject** of a plan may transition `parked → queued` (revise) or `parked → cancelled` / `failed` depending on approval policy on the task.

### 3.4 Park reasons

```text
park_reason ∈ {
  plan_approval,
  agent_question,
  user_guidance_required,
  external_input_required
}
```

---

## 4. Services and trust boundaries

```text
                    untrusted public
  GitHub/Slack/CI ──────────────────► [webhook]
                                            │
  browsers / CLI  ─ auth ──────────────► [api] ◄── [ui]
                                            │
                         Postgres ◄─────────┤
                         outbox ──────────► NATS
                                            │
                         trusted cluster ──► [harness]
                                            │
                               LLM providers and/or optional LiteLLM
                               · Daytona · agent-sandbox · OpenSandbox
```

| Service | Trust | Outbound |
|---|---|---|
| `webhook` | Public ingress; signature verify only | Postgres, outbox publisher |
| `api` | Session/JWT/OIDC; org RBAC | Postgres, outbox, SSE to clients |
| `harness` | Cluster-internal; service auth to API optional | Postgres, NATS, LLM (direct and/or LiteLLM), selected sandbox provider, GitHub/Slack APIs as needed |
| `ui` | Static/SPA; calls `api` only | `api` |
| `litellm` (optional) | Cluster-internal model gateway | Upstream model providers |
| sandbox control planes | Provider-specific (Daytona / agent-sandbox / OpenSandbox) | Workload runtimes |

Harness **must not** expose a public HTTP API for task creation.
Optional internal admin endpoints (health, metrics) only.

LiteLLM is **optional**.
When disabled, harness calls provider APIs directly.
Sandbox control planes may be co-located in-cluster or external SaaS.

---

## 5. PostgreSQL ownership (logical schema)

Not a migration file — contract-level tables.
Exact DDL is implementation.

### 5.1 Tenancy & config

- `orgs`, `users`, `org_memberships`
- `user_identities` (GitHub login, Slack id, email)
- `org_settings` (defaults: `default_model`, `default_sandbox_provider=agent_sandbox`, agent policy, enabled sandbox providers, MCP policy)
- `user_settings` (optional overrides: preferred `model`, preferred `sandbox_provider`, default MCP enable set)
- `secrets` / token rows (encrypted at rest; app DEK via KMS or envelope keys) — includes per-org LiteLLM virtual keys if used, per-provider sandbox credentials, and **MCP auth material** (tokens, headers) referenced by `mcp_servers`
- **`mcp_servers`** — user- and org-scoped MCP server definitions (§11)
- **`mcp_server_assignments`** (optional) — which servers attach by default to which `agent_type` / repos

### 5.2 Work

- `tasks` — `task_id`, `org_id`, `thread_id`, `source`, `source_ref`, `agent_type`, `status`, `title`, `repo`, `created_by`, timestamps, `cancel_requested_at`, `park_reason`, `active_run_id`, **`model`**, **`sandbox_provider`**, **`mcp_server_ids`** (UUID[] resolved at create / start; snapshot of which MCP servers apply to this task)
- `runs` — `run_id`, `task_id`, `status`, `attempt`, `worker_id`, `lease_expires_at`, `started_at`, `ended_at`, `error_code`, `error_message`
- `messages` — ordered conversation / control inputs (`role`, `content`, `blocks`, `visibility`)
- `approvals` — `approval_id`, `task_id`, `run_id`, `kind`, `payload`, `status` (`pending|approved|rejected|expired`), `decided_by`, `decided_at`
- `run_events` — append-only UI/audit stream (`seq`, `type`, `payload`, `created_at`)
- `sandboxes` — `task_id`, **`provider`** (`daytona` \| `agent_sandbox` \| `opensandbox`), `sandbox_id`, `status`, `last_seen_at`, `expires_at`, `provider_metadata` (JSONB)

### 5.3 Reliability

- `webhook_deliveries` — `(provider, delivery_id)` unique; raw envelope hash; processed_at
- `outbox` — `id`, `aggregate_type`, `aggregate_id`, `subject`, `payload`, `created_at`, `published_at`
- `processed_messages` — NATS `msg_id` / business key processed
- `command_idempotency` — client `Idempotency-Key` for mutating API calls

### 5.4 Graph state (harness → Postgres, required)

The harness **must** durable-checkpoint agent/graph state to **Postgres** so any eligible worker can resume after park, crash, or lease reaper requeue.

Allowed implementations (both are Postgres-backed):

- **LangGraph Postgres checkpointer** tables (preferred when using LangGraph/Deep Agents), or
- Opaque app table e.g. `graph_checkpoints(task_id, run_id, thread_key, version, blob, metadata, created_at)` with monotonic version per run.

**Requirements:**

| Requirement | Contract |
|---|---|
| Durability target | Postgres only (not local pod FS, not NATS, not sandbox disk alone) |
| When to write | At least: after each successful model/tool step boundary; before `parked`; before graceful cancel terminalization |
| Resume | New claim loads latest checkpoint for `task_id`/`run_id` and continues |
| Failure | If checkpoint write fails, treat step as failed; do not advance lease semantics as if durable |
| Product status | App code reads task/run status from app tables (`tasks`/`runs`), not by scraping checkpoints |

Sandbox workspace state is **not** a substitute for graph checkpoints; sandbox may be recreated, checkpoint must still restore control flow.

---

## 6. HTTP contracts — `api`

Base path: `/v1` Auth: `Authorization: Bearer <token>` or session cookie (UI).
Errors: RFC 7807-ish `{ "type", "title", "status", "detail", "error_code" }`.
Idempotent POSTs: header `Idempotency-Key: <string>` (required for create/cancel/approve).

### 6.1 Tasks

#### `POST /v1/tasks`

Create a task from API or UI.

```json
{
  "agent_type": "coding",
  "title": "Fix flaky auth test",
  "repo": "acme/payments",
  "base_ref": "main",
  "prompt": "…",
  "source": "web_ui",
  "model": "gpt-4.1",
  "sandbox_provider": "daytona",
  "metadata": {}
}
```

| Field | Required | Notes |
|---|---|---|
| `model` | no | If omitted → user default → org `default_model` → platform default |
| `sandbox_provider` | no | If omitted → user default → org `default_sandbox_provider` → configured platform default (**`agent_sandbox`** when unset). Must be in org-enabled set |

Response `201`:

```json
{
  "task_id": "…",
  "thread_id": "…",
  "status": "queued",
  "active_run_id": null,
  "model": "gpt-4.1",
  "sandbox_provider": "daytona"
}
```

Effects: resolve model + sandbox_provider → insert `tasks` + initial user `messages` + outbox `tasks.enqueue`.
Errors: `400 unknown_sandbox_provider`, `403 sandbox_provider_disabled`, `400 unknown_model` (if not in org allowlist when allowlisting is on).

#### `GET /v1/tasks/{task_id}`

Full task + active run summary + park/approval summary.

#### `GET /v1/tasks`

Query: `status`, `repo`, `source`, `cursor`, `limit`.

#### `POST /v1/tasks/{task_id}/messages`

Guide / answer while running or parked.

```json
{
  "content": "Use the existing retry helper in pkg/http",
  "kind": "user_guidance"
}
```

`kind ∈ { user_guidance, user_answer, system }`.

Effects:

- append `messages`
- if `status=parked` and message unparks → `parked → queued` + outbox enqueue
- if `status=running` → outbox `tasks.control` (deliver mid-loop)
- if terminal → `409 task_terminal`

#### `POST /v1/tasks/{task_id}/cancel`

```json
{ "reason": "user_requested" }
```

Effects: set `cancel_requested_at`; if `queued|parked` → `cancelled`; if `running` → outbox control `cancel` (harness must observe within lease/heartbeat).

#### `GET /v1/tasks/{task_id}/events`

Cursor pagination over `run_events` (`after_seq`, `limit`).

#### `GET /v1/tasks/{task_id}/events/stream`

SSE: `event: run_event` data = event JSON; also `event: task_status` on status change.
Reconnect with `Last-Event-ID` = last `seq`.

### 6.2 Approvals

#### `GET /v1/tasks/{task_id}/approvals/pending`

#### `POST /v1/approvals/{approval_id}/decision`

```json
{
  "decision": "approved",
  "comment": "LGTM, ship as draft PR"
}
```

`decision ∈ { approved, rejected }`.

Effects: update approval; append message; transition task per policy; enqueue resume if needed.

### 6.3 Runs (read-mostly)

#### `GET /v1/tasks/{task_id}/runs`

#### `GET /v1/runs/{run_id}`

Includes lease worker id (admin), timestamps, error.

### 6.4 Admin / config (sketch)

- `GET/PATCH /v1/orgs/{org_id}/settings` — includes `default_model`, `default_sandbox_provider` (default **`agent_sandbox`**), `enabled_sandbox_providers`, MCP org policy
- `GET/PUT /v1/orgs/{org_id}/models` — org allowlist / aliases for model ids (direct `provider:model` and/or LiteLLM aliases)
- `GET /v1/sandbox-providers` — catalog of providers available to the caller (`daytona`, `agent_sandbox`, `opensandbox`) with enabled flags
- `GET/PATCH /v1/me/settings` — user preferred `model`, preferred `sandbox_provider`, default MCP server ids
- **MCP servers** — full CRUD in §6.6
- User mappings, repo enablement: preserve current product behavior behind these routes

### 6.5 Health

- `GET /healthz` — process up
- `GET /readyz` — Postgres (+ NATS publisher path) reachable

### 6.6 MCP servers (user-configurable)

Users and org admins configure MCP servers via the API (and UI).
Auth secrets are write-only after create (never returned in full).

#### Scope

| `scope` | Who manages | Visibility |
|---|---|---|
| `user` | Owning user | That user (+ harness on their runs) |
| `org` | Org admin | All org members per policy; may be marked `required` for agent types |

#### `GET /v1/mcp-servers`

Lists servers visible to the caller (own user + enabled org servers).
Query: `scope=user|org`, `enabled=true`.

Response items omit secret values; include `has_auth: true|false`, `status`, tool count if last probe known.

#### `POST /v1/mcp-servers`

```json
{
  "scope": "user",
  "name": "corp-docs",
  "transport": "sse",
  "url": "https://mcp.example.com/sse",
  "headers": { "Authorization": "Bearer …" },
  "auth": {
    "type": "bearer",
    "token": "…"
  },
  "enabled": true,
  "tool_allowlist": ["search_docs", "get_page"],
  "tool_denylist": [],
  "agent_types": ["coding", "chat"],
  "execution": "harness"
}
```

| Field | Required | Notes |
|---|---|---|
| `scope` | yes | `user` \| `org` |
| `name` | yes | Unique per owner scope |
| `transport` | yes | `stdio` \| `sse` \| `streamable_http` (MCP transports the harness supports) |
| `url` | if remote | Required for `sse` / `streamable_http` |
| `command` + `args` | if stdio | Harness-side process only; **org policy** may disable stdio |
| `headers` / `auth` | no | Stored encrypted; redacted on read |
| `enabled` | no | default `true` |
| `tool_allowlist` / `tool_denylist` | no | Empty allowlist = all tools from server (minus denylist) |
| `agent_types` | no | Empty = all agent types |
| `execution` | no | default **`harness`**; `sandbox` only if org allows and transport is compatible |

Response `201`: server metadata without secrets.

#### `GET /v1/mcp-servers/{mcp_server_id}`

#### `PATCH /v1/mcp-servers/{mcp_server_id}`

Update name, url, enable flag, tool filters, agent_types, rotate auth (`auth` replaces prior secret).

#### `DELETE /v1/mcp-servers/{mcp_server_id}`

Soft-delete or hard-delete; in-flight tasks keep the **snapshotted** `mcp_server_ids` + resolved config frozen at run start (see §11).

#### `POST /v1/mcp-servers/{mcp_server_id}/test`

Harness or API probes connectivity and lists tools (timeout bounded).
Returns `{ "ok": true, "tools": ["…"] }` or error detail.
Does not expose secrets.

#### Task create optional override

`POST /v1/tasks` may include:

```json
{
  "mcp_server_ids": ["…", "…"],
  "mcp_mode": "replace"
}
```

| `mcp_mode` | Meaning |
|---|---|
| `inherit` (default) | Resolve from user defaults + org required/enabled servers |
| `replace` | Use only the provided ids (must be visible to user) |
| `append` | Inherit + extra ids |

---

## 7. HTTP contracts — `webhook`

Public base: `/hooks` No user auth.
Provider signature required.
Responses: fast `2xx` after durable accept (DB write), **not** after agent completion.

### 7.1 Endpoints

| Method | Path | Provider |
|---|---|---|
| `POST` | `/hooks/github` | GitHub App / webhooks |
| `POST` | `/hooks/slack` | Slack Events API |
| `POST` | `/hooks/slack/interactions` | Slack interactivity (optional) |

CI may arrive as GitHub `check_run` / `check_suite` / `workflow_run` / `status` on the GitHub endpoint.

### 7.2 Accept contract

For every delivery:

1. Verify signature; on failure → `401`/`403`, no side effects.
1. Compute `idempotency_key = provider + ":" + delivery_id` (GitHub `X-GitHub-Delivery`, Slack `event_id`, etc.).
1. `INSERT` into `webhook_deliveries`; on conflict → `200` with `{ "duplicate": true }` (ack for provider retry).
1. Normalize → domain command (see §7.3).
1. In **one transaction**: upsert task / append messages / set cancel / create approval decision as needed + outbox row(s).
1. Return `202` or `200` `{ "accepted": true, "task_id": "…" }`.

Webhook service **does not** call harness, LiteLLM, or sandboxes.

### 7.3 Normalized ingress commands

Internal shape (stored + outbox payload):

```json
{
  "schema_version": 1,
  "command": "upsert_and_enqueue",
  "org_id": "…",
  "source": "github_issue",
  "source_ref": "acme/payments#42",
  "thread_id": "gh:acme/payments:issue:42",
  "agent_type": "coding",
  "actor": { "kind": "github_user", "id": "alice" },
  "repo": "acme/payments",
  "title": "…",
  "message": {
    "kind": "user_input",
    "content": "…",
    "blocks": []
  },
  "idempotency_key": "github:abc-delivery-id",
  "metadata": {
    "issue_number": 42,
    "html_url": "…"
  }
}
```

| `command` | When |
|---|---|
| `upsert_and_enqueue` | New work or follow-up that should run/resume |
| `append_message` | Mid-run guidance; may not change status if already running |
| `request_cancel` | Explicit cancel from emoji/command/UI mirror |
| `ci_failure` | CI autofix path; policy gate before enqueue |
| `ignore` | Bot loops, disabled repo, unauthorized actor (still recorded) |

Policy (enabled repos, user allowlist, autofix flags) runs in `webhook` **or** shared library used by `webhook`+`api` — must be identical.

---

## 8. NATS JetStream contracts

### 8.1 Streams

| Stream | Retention | Subjects | Purpose |
|---|---|---|---|
| `TASKS` | WorkQueue | `tasks.enqueue.>` | Runnable work for harness |
| `CONTROL` | Limits + interest | `tasks.control.>` | Cancel / mid-run messages to active workers |
| `EVENTS` | Limits (time/size) | `tasks.events.>` | Fan-out progress (optional; API may use LISTEN/SSE from DB only) |
| `DLQ` | Limits | `dlq.>` | Poison messages |

Recommend **outbox relay** publishes with `Nats-Msg-Id` = outbox id (dedup).

### 8.2 Subject layout

```text
tasks.enqueue.{org_id}.{agent_type}.{task_id}
tasks.control.{task_id}
tasks.events.{task_id}
dlq.tasks
dlq.control
```

Consumers should not require `org_id` in subscription for v1 global pools; subject fields support future sharding and stream filtering.

### 8.3 Message envelope (all subjects)

```json
{
  "schema_version": 1,
  "msg_id": "outbox-uuid-or-nats-msg-id",
  "type": "task.enqueue",
  "occurred_at": "2026-07-25T12:00:00Z",
  "org_id": "…",
  "task_id": "…",
  "run_id": null,
  "payload": {}
}
```

### 8.4 `task.enqueue` payload

Published when a task becomes **runnable** (`queued`).

```json
{
  "type": "task.enqueue",
  "payload": {
    "task_id": "…",
    "agent_type": "coding",
    "reason": "created | resumed | redelivered | manual_redrive",
    "priority": 50,
    "not_before": null,
    "trace_id": "…"
  }
}
```

**Consumer:** harness worker pool (competing consumers).
**Ack policy:** Ack **only after** successful lease acquisition in Postgres **or** after determining the message is obsolete (task not `queued` / already leased).
If lease fails because another worker won, ack (no redelivery storm).
If worker crashes after lease, **lease expiry** requeues (see §9) — do not rely on NATS redelivery alone for long runs.

### 8.5 `task.control` payload

```json
{
  "type": "task.control",
  "payload": {
    "task_id": "…",
    "run_id": "…",
    "control": "cancel | append_message | approval_decided",
    "message_id": "…",
    "approval_id": "…",
    "cancel_reason": "user_requested"
  }
}
```

**Consumer:** the worker that holds the lease (subscription with filter, or all workers ignore if `run_id`/lease owner mismatch).
**Semantics:** best-effort wake; **Postgres remains authoritative** (worker also polls cancel/messages every step).

### 8.6 `task.event` payload (optional bus)

Mirrors `run_events` for push fan-out.
API may ignore and read DB only.

```json
{
  "type": "task.event",
  "payload": {
    "event_id": "…",
    "seq": 18,
    "run_id": "…",
    "event_type": "tool_started",
    "data": { "tool": "execute", "summary": "pytest -q" }
  }
}
```

### 8.7 DLQ

After `max_deliver` on enqueue processing failures (poison payload, repeated handler bugs), publish to `dlq.tasks` with original envelope + error.
Ops redrive → new outbox enqueue after fix.

---

## 9. Harness worker contract

### 9.1 Process model

- Deployment scaled by KEDA (JetStream lag and/or Prometheus `tasks_queued` count).
- Each pod: **concurrency = 1** active run.
- Entry: consume `tasks.enqueue.*` → claim → execute → release.

### 9.2 Claim / lease algorithm

```text
on message task.enqueue(task_id):
  BEGIN
    SELECT task FOR UPDATE
    IF status != queued: COMMIT; ACK msg; return
    IF cancel_requested: set cancelled; COMMIT; ACK; return
    insert/update run as leased/active
    set task.status = running, active_run_id, worker_id, lease_expires_at = now()+LEASE
  COMMIT
  ACK msg
  run agent loop with heartbeats
```

| Parameter | Suggested default |
|---|---|
| `LEASE_TTL` | 60s |
| `HEARTBEAT_INTERVAL` | 15s |
| `MAX_RUN_DURATION` | org/policy (e.g. 4h) |
| `STEP_MESSAGE_POLL` | every tool/model boundary |

Heartbeat: `UPDATE runs SET lease_expires_at = now()+LEASE WHERE run_id AND worker_id`.

**Reaper** (API sidecar or harness loop / cron Deployment):

```text
IF status=running AND lease_expires_at < now():
  mark run failed_or_interrupted
  task.status = queued  (retry) OR failed (budget exceeded)
  outbox task.enqueue if retry
```

### 9.3 Agent loop obligations

Each step boundary the harness MUST:

1. Refresh lease heartbeat.
1. Load new `messages` with `id > last_seen`.
1. If `cancel_requested` → abort tools, best-effort sandbox cleanup policy, terminal `cancelled`.
1. **Persist graph checkpoint to Postgres** (required; see §5.4).
   Do not complete the step boundary until the checkpoint write succeeds.
1. Append `run_events` for UI (tool start/end, PR opened, errors).
1. If agent emits plan/question requiring human → create `approvals` / park message → `park` (§9.4).

### 9.4 Park protocol

```text
BEGIN
  checkpoint graph state to Postgres  -- MUST succeed before park is visible
  task.status = parked
  task.park_reason = …
  run.status = parked
  clear lease (worker_id null, lease_expires_at null)
  persist sandbox row (keep warm if policy allows)
COMMIT
exit process slot (pod free for other work)
```

If the Postgres checkpoint write fails, the harness must **not** transition to `parked` or release the lease as a clean park; surface `run_events.error` and follow failure/retry policy.

Resume path is **only** via API/webhook decision/message → `parked → queued` → new `task.enqueue`.
Claiming worker **loads the latest Postgres checkpoint** for the run and continues.

**Contract choice (v1):** continue same `run_id` across park/resume; `attempt` unchanged; `runs.status` flips `parked ↔ active`.

### 9.5 Cancel protocol

1. API/webhook sets `tasks.cancel_requested_at` (source of truth).
1. Control message optional wake.
1. Harness observes → stop scheduling new model calls → mark run+task `cancelled` → sandbox per policy (`stop` vs `delete`).
1. If already parked/queued, API may terminalize without harness.

### 9.6 Agent type registry

```text
agent_type ∈ { coding, reviewer, analyzer, chat, ... }
```

```text
harness/registry:
  coding    -> create_coding_agent(ctx)
  reviewer  -> create_reviewer_agent(ctx)
  analyzer  -> create_analyzer_agent(ctx)
  chat      -> create_chat_agent(ctx)
```

Job payload / task row selects factory.
Unknown `agent_type` → fail run `error_code=unknown_agent_type` (no retry).

### 9.7 Execution context passed into agent

Harness builds immutable-ish `RunContext`:

```json
{
  "org_id": "…",
  "task_id": "…",
  "run_id": "…",
  "thread_id": "…",
  "agent_type": "coding",
  "repo": "acme/payments",
  "base_ref": "main",
  "model": "gpt-4.1",
  "litellm": {
    "base_url": "http://litellm:4000",
    "api_key_secret_ref": "org/litellm-key"
  },
  "sandbox": {
    "provider": "daytona",
    "sandbox_id": null,
    "provider_config_secret_ref": "org/sandbox-daytona"
  },
  "mcp_servers": [
    {
      "mcp_server_id": "…",
      "name": "corp-docs",
      "transport": "sse",
      "url": "https://mcp.example.com/sse",
      "execution": "harness",
      "tool_allowlist": ["search_docs"],
      "auth_secret_ref": "mcp/…"
    }
  ],
  "source": "github_issue",
  "permissions": {
    "open_pr": true,
    "push": true
  },
  "trace_id": "…"
}
```

Models: **direct providers and/or optional LiteLLM** (§10).
Sandboxes: **only** via `SandboxProvider` interface for `daytona` | `agent_sandbox` | `opensandbox` (§10). Provider is taken from the task row (user-selected at create); harness must not override except on explicit admin redrive.
MCP: load `mcp_servers` from task snapshot (§11); connect at run start; expose filtered tools to the agent.

### 9.8 Side effects (outbound)

Harness may call:

- LLM providers (OpenAI / Anthropic / Fireworks / Google) and/or LiteLLM proxy
- Selected sandbox provider control plane only (`daytona` **or** `agent_sandbox` **or** `opensandbox` for that task)
- **Configured MCP servers** (tools/resources) for this run
- GitHub / Slack APIs for progress (or publish events and let a future `notifier` own it)

**v1 allows harness to notify Slack/GitHub directly** for lower latency; must still write the same facts to `run_events` / messages for UI.

### 9.9 Health / metrics

- `GET /healthz`, `GET /readyz` (NATS + Postgres)
- Metrics: `harness_runs_active`, `harness_lease_renew_failures`, `harness_step_duration`, `harness_parks_total`, `harness_cancels_total`

---

## 10. Models (direct + optional LiteLLM) and sandboxes (multi-provider)

### 10.1 LLM routing (LiteLLM optional)

| Rule | Contract |
|---|---|
| Modes | `LLM_MODE=auto\|direct\|litellm`. **`auto`** (default): use LiteLLM only if `LITELLM_ENABLED=true`; else direct providers when keys are present; else LiteLLM if `LITELLM_BASE_URL` is set. |
| Direct providers | Harness calls **OpenAI**, **Anthropic**, **Fireworks**, **Google** HTTP APIs. Model ids: `openai:…`, `anthropic:…`, `fireworks:…`, `google:…` (or bare name + `DEFAULT_LLM_PROVIDER`). |
| Optional LiteLLM | When enabled, harness uses OpenAI-compatible `/v1/chat/completions` against `LITELLM_BASE_URL`. Not required for local or prod. |
| Multi-model | Org may enable many models; user/API may pick per task. Resolution: request → user default → org default → platform default. |
| Secrets | Provider keys and LiteLLM keys live in env/secret store on the **harness** process — never in the sandbox. |
| Failures | `429`/`5xx` → retry with backoff inside the run; surface `model_*` / `error` events; do not switch sandbox. |
| Deploy | Direct mode needs only provider API keys. LiteLLM (if used) is a separate optional Service. |

API surfaces for model config: §6.4 (`/v1/orgs/.../models`, settings, `/v1/me/settings`).

### 10.2 Sandbox providers (required set)

Supported `sandbox_provider` values:

| Provider id | Meaning | Default? |
|---|---|---|
| `daytona` | [Daytona](https://www.daytona.io/) sandboxes | No |
| `agent_sandbox` | [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) (in-cluster agent sandboxes) | **Yes — platform & org default** |
| `opensandbox` | [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) control plane / SDK | No |

All three **must** be implemented behind one harness interface so agents are provider-agnostic.

### 10.3 User / org selection

Resolution order for `sandbox_provider` at task create (API, UI, or normalized webhook policy):

1. Explicit request field / UI choice / source command override (if allowed)
1. User preference (`user_settings.sandbox_provider`)
1. Org default (`org_settings.default_sandbox_provider`)
1. Configured platform default (`DEFAULT_SANDBOX_PROVIDER`) → **`agent_sandbox`** when unset

Constraints:

- Provider must be in `org_settings.enabled_sandbox_providers` (default enabled set may be all three or only those with credentials configured).
- Missing credentials for the selected provider → task create fails fast (`503 sandbox_provider_unavailable`) or webhook records `ignore` / failed accept with clear error event — do not silently fall back to another provider unless org policy `sandbox_fallback=true` (off by default).
- **Sticky per task:** once set on `tasks.sandbox_provider`, park/resume and retries reuse the same provider and prefer reconnecting `sandboxes.sandbox_id`.

Webhook-created tasks: use org/user defaults unless the ingress command includes an explicit provider (admin/advanced).

### 10.4 `SandboxProvider` interface (harness contract)

```text
create(ctx) -> sandbox_ref
connect(ctx, sandbox_id) -> sandbox_ref
exec(sandbox_ref, command, opts) -> result
read_file / write_file / list (or equivalent FS ops)
heartbeat(sandbox_ref)   # optional per provider
stop(sandbox_ref)
delete(sandbox_ref)
```

| Obligation | Contract |
|---|---|
| Registry | `daytona` · `agent_sandbox` · `opensandbox` registered in harness |
| Selection | `providers.get(task.sandbox_provider)` only |
| Persist | Write `sandboxes` row (`provider`, `sandbox_id`, status) to Postgres on create/reuse |
| Resume | `connect` existing id; on failure `create` new + `sandbox_created` event |
| Credentials | Provider tokens/kubeconfig stay on harness/platform secret mount — **not** in agent-visible env unless scoped short-lived inject |
| GC | Terminal/expired tasks: stop/delete per org policy; reaper scans `sandboxes` |

Provider-specific config (namespaces, snapshots, resource classes, OpenSandbox endpoint, Daytona API URL) lives in org/platform secrets + settings — not hard-coded in agent graphs.

### 10.5 UI / API product requirements

- Task create UI: **model** picker (provider catalog and/or LiteLLM aliases) and **sandbox** picker (configured default selected).
- Org admin: enable/disable providers, set the default provider, and configure credentials status (configured / missing).
- Task detail: show resolved `model` + `sandbox_provider` + sandbox lifecycle events.

### 10.6 Non-goals for providers

- Running the same task on two providers concurrently
- Transparent cross-provider sandbox migration mid-run (v1)
- Embedding provider SDKs inside the **sandbox** image for control-plane ops (control plane calls are harness-side)

---

## 11. MCP servers (user-configurable)

Users (and org admins) configure **Model Context Protocol** servers so agents gain extra tools (docs, internal APIs, observability, etc.) without code changes.

### 11.1 Ownership model

| Scope | Configured by | Typical use |
|---|---|---|
| **User** | End user in UI/API | Personal MCP servers (private tokens) |
| **Org** | Admin | Shared company MCP (Corridor, Datadog, internal RAG, etc.) |

Org policy may:

- Allow/deny **user-scoped** MCP entirely
- Allow/deny **`stdio`** transport (default: **deny** in multi-tenant prod; allow in trusted single-tenant)
- Force **org-required** servers onto every run of given `agent_type`s
- Cap max servers per run (suggested default: 10)

### 11.2 Logical record (`mcp_servers`)

```text
mcp_server_id
owner: { scope: user|org, user_id?, org_id }
name
transport: stdio | sse | streamable_http
url? | command? + args? + env_secret_refs?
auth_secret_ref?          # encrypted bearer / headers / OAuth material
headers_redacted preview
enabled
tool_allowlist[]
tool_denylist[]
agent_types[]             # empty = all
execution: harness | sandbox   # default harness
created_at, updated_at, created_by
last_test_at, last_test_ok, last_tools[]
```

Secrets live in the secrets table / KMS envelope — **never** in `run_events`, logs, or API GET bodies.

### 11.3 Resolution at task create / run start

```text
resolved = []
+ org servers marked required for agent_type
+ user default mcp_server_ids (if mcp_mode=inherit|append)
+ request mcp_server_ids (replace|append)
filter: enabled, visible to actor, agent_type match, org allow
dedupe by mcp_server_id
cap by org max
snapshot onto tasks.mcp_server_ids (+ optional frozen config blob on run)
```

**Snapshot:** harness uses the snapshot from task/run start so mid-run admin edits do not change an active job.
Edits apply to **subsequent** tasks/runs.

### 11.4 Runtime (harness)

| Step | Contract |
|---|---|
| Connect | After lease, before agent loop: connect each MCP server; on failure → `mcp_error` event; policy `fail_open` (skip server) vs `fail_closed` (fail run) — **default `fail_open` for optional user servers, `fail_closed` for org-required** |
| Tools | List tools; apply allow/deny lists; register with Deep Agent / LangChain tool surface under a namespaced name `mcp_{server}_{tool}` (or equivalent collision-safe scheme) |
| Calls | Tool invocations emit `tool_started` / `tool_finished` with `mcp_server_id`; timeouts enforced |
| Execution locus | **`harness` (default):** MCP client in harness process; credentials never enter sandbox. **`sandbox`:** only if org allows; still no long-lived master secrets in repo checkout |
| Park/resume | Disconnect on park if desired; reconnect on resume from snapshot |
| Cancel/terminal | Close MCP sessions |

### 11.5 Security

- Treat MCP tool results as **untrusted content** (prompt-injection surface), same class as `fetch_url` / web search.
- SSRF controls on remote `url` (block link-local/metadata IPs unless org admin allowlist).
- Org admins can require URL allowlist prefixes for org- and user-scoped remote MCP.
- Audit: who created/updated server configs; do not audit raw secrets.
- Rate-limit `POST .../test` and concurrent MCP sessions per org.

### 11.6 UI product requirements

- Settings → **MCP servers**: list, add, edit, enable/disable, test connection, show discovered tools.
- Org admin: org-scoped servers, required flags, transport policy, URL allowlists.
- Task create: optional multi-select of MCP servers (defaults pre-selected from user settings).
- Task detail: which MCP servers were attached + connection errors.

### 11.7 Non-goals (v1)

- Marketplace auto-install of arbitrary MCP packages without admin review
- User-defined MCP that runs privileged cluster APIs without org policy
- Hot-reload of MCP config into an already `running` task without restart/resume boundary

---

## 12. Event types (`run_events`)

Stable `event_type` strings for UI and integrations:

| event_type | When |
|---|---|
| `task_created` | Task inserted |
| `run_leased` | Worker claimed |
| `run_started` | Agent loop began |
| `model_started` / `model_finished` | LLM call boundary |
| `tool_started` / `tool_finished` | Tool call boundary (includes MCP tools) |
| `mcp_connected` / `mcp_error` / `mcp_tools_loaded` | MCP session lifecycle |
| `message_received` | User/control message applied |
| `plan_proposed` | Plan awaiting approval |
| `approval_decided` | Human decision recorded |
| `question_asked` | Agent parked on question |
| `parked` / `resumed` | Lifecycle |
| `pr_opened` / `pr_updated` | GitHub PR side effect |
| `sandbox_created` / `sandbox_reused` / `sandbox_error` | Sandbox lifecycle |
| `cancel_requested` / `cancelled` | Cancel path |
| `run_succeeded` / `run_failed` | Terminal attempt |
| `error` | Non-terminal error detail |

Payloads are JSON; additive fields OK.
Renames require new `event_type` or schema version.

---

## 13. Idempotency matrix

| Entry point | Key | On duplicate |
|---|---|---|
| GitHub webhook | `X-GitHub-Delivery` | Return prior accept; no new outbox |
| Slack event | `event_id` | Same |
| API `POST /tasks` | `Idempotency-Key` | Return original `task_id` |
| API cancel/message/approve | `Idempotency-Key` | Return original result |
| NATS enqueue | `Nats-Msg-Id` / outbox id | JetStream + `processed_messages` |
| Lease claim | task row lock | Loser acks enqueue msg |

**Exactly-once is not promised.**
**At-least-once + idempotent handlers** is the contract.

---

## 14. Authorization (brief)

| Action | Who |
|---|---|
| Create task via API/UI | Org member with `tasks:write` |
| Message / cancel / approve | Task creator, org admin, or mapped identity from source |
| Webhook actor | Must pass org allowlist / enabled repo / linking map |
| CRUD user MCP servers | Owning user |
| CRUD org MCP servers | Org admin |
| Attach MCP to task | Caller must have visibility on each `mcp_server_id` |
| Harness DB access | Service role; row scope by claimed `task_id` only; may read MCP secrets for snapshotted servers only |
| Cross-org | Forbidden |

Webhook forges are mitigated by signatures + allowlists, not by secrecy of URLs alone.

---

## 15. Failure modes & expected behavior

| Failure | Behavior |
|---|---|
| Webhook DB down | `503`; provider retries |
| Outbox publisher lag | Tasks exist but delayed start; metrics alert |
| Worker OOM mid-run | Lease expires → reaper requeues or fails per retry policy |
| Sandbox dead | Harness recreates; event `sandbox_created`; continue or fail if budget exceeded |
| LLM 429 (direct or LiteLLM) | Retry with backoff inside run; don't release lease |
| Optional MCP connect fail | `mcp_error` event; skip server (`fail_open`); run continues |
| Required MCP connect fail | Fail run (`fail_closed`); task may requeue per policy |
| Human never approves | Approval `expires_at` → task `failed` or `cancelled` per policy |
| Duplicate enqueue | Claim no-op; ack |
| Control for wrong run | Ignore; DB poll still correct |

### Retry policy (v1 defaults)

- Max **3** automatic requeues after lease loss / infra failure.
- No auto-retry on `unknown_agent_type`, authz failure, invalid repo.
- User cancel never retries.

---

## 16. KEDA / scale signals

| Component | Scale trigger |
|---|---|
| `webhook` | RPS / CPU |
| `api` | RPS / CPU / SSE connections |
| `harness` | `queued` task count (Prometheus) and/or JetStream `TASKS` pending |
| `ui` | standard front-end HPA or serverless |

Harness min=0 allowed if cold start + sandbox create latency acceptable; else min≥1.

---

## 17. Versioning

- HTTP: URL `/v1`; additive JSON fields non-breaking.
- NATS: `schema_version` integer; consumers accept N and N-1 during rollout.
- State machine: new statuses require dual-write/read window and doc update.

---

## 18. Non-goals (v1)

- Multi-run parallel workers on one `task_id`
- NATS as system of record
- Per-step ScaledJobs
- Harness public API
- Requiring LiteLLM for all deployments (direct providers are first-class)
- Silent cross-provider sandbox fallback (unless org enables it)
- Perfect live token streaming (structured `run_events` first)
- Hot-reload of MCP config into a running task without park/resume boundary

---

## 19. Open implementation choices (explicitly deferred)

| Topic | Options | Default if unspecified |
|---|---|---|
| Worker shape | Deployment vs ScaledJob | **Deployment + KEDA**, concurrency 1 |
| Park sandbox | Keep warm vs destroy | **Keep warm** with TTL (e.g. 60m) |
| Notifier | In-harness vs service | **In-harness** for Slack/GitHub v1 |
| Checkpoint **format** | LG SQL checkpointer tables vs opaque `graph_checkpoints` blob | **LG Postgres checkpointer** preferred; either is fine **as long as durability is Postgres** |
| SSE source | Postgres poll vs NATS | **Postgres poll / LISTEN** |
| Org enabled sandboxes | subset vs all three | All **configured** providers; default selection **`agent_sandbox`** |
| Sandbox fallback on create failure | fail vs try next provider | **Fail** (no silent fallback) |
| MCP optional connect failure | fail_open vs fail_closed | **fail_open** (user/optional); **fail_closed** (org-required) |
| MCP stdio transport | allow vs deny | **Deny** by default in multi-tenant; org opt-in |

**Not deferred:**

- Harness checkpoint durability **must** be Postgres (§1.2 / §5.4 / §9.3)
- LLM via **direct providers and/or optional LiteLLM** (§1.9 / §10.1)
- Sandbox providers **`daytona` | `agent_sandbox` | `opensandbox`**, user-selectable, configured platform default with **`agent_sandbox`** as the fallback (§1.10 / §10)
- **User/org-configurable MCP servers** loaded by harness (§1.11 / §6.6 / §11)

---

## 20. Acceptance checklist (contract completeness)

A first vertical slice is “done” when:

- [ ] `POST /v1/tasks` creates `queued` task + outbox + NATS enqueue
- [ ] Harness claims with lease; status `running`
- [ ] Harness writes graph checkpoints to Postgres each step; kill pod mid-run and resume from checkpoint on requeue
- [ ] Park only after successful Postgres checkpoint; resume loads same checkpoint
- [ ] Mid-run `POST .../messages` appears before next model call
- [ ] Plan park releases worker; approval resumes single task
- [ ] Cancel from API stops running work and is terminal
- [ ] Duplicate GitHub delivery does not double-enqueue
- [ ] Worker kill → lease expiry → requeue ≤ max retries
- [ ] UI can stream `run_events` via SSE
- [ ] Second `agent_type` registers without new deployable
- [ ] Task create selects model; harness works with `LLM_MODE=direct` without LiteLLM
- [ ] Optional `LLM_MODE=litellm` / `LITELLM_ENABLED=true` routes through LiteLLM
- [ ] Task create selects sandbox provider; configured platform default is used when omitted
- [ ] Harness runs same agent against `daytona`, `agent_sandbox`, and `opensandbox` via provider interface
- [ ] Park/resume reconnects the same provider + sandbox id when warm
- [ ] User can CRUD MCP servers via API/UI; secrets never returned on GET
- [ ] Harness attaches resolved MCP tools for a run; tool calls emit events
- [ ] Org-required MCP fail_closed; optional user MCP fail_open by default

---

## Document history

| Version | Date | Notes |
|---|---|---|
| 0.1 | 2026-07-25 | Initial multi-service contracts from architecture decision set |
| 0.2 | 2026-07-25 | Hard requirement: harness persist/checkpoint graph state to Postgres |
| 0.3 | 2026-07-25 | LiteLLM required; sandboxes daytona (default), agent_sandbox, opensandbox; user-selectable |
| 0.4 | 2026-07-25 | User/org-configurable MCP servers (API, snapshot, harness runtime, security) |
| 0.5 | 2026-07-25 | LiteLLM optional; direct OpenAI/Anthropic/Fireworks/Google in harness |
| 0.6 | 2026-08-15 | Sandbox fallback made configuration-driven; built-in default changed to agent_sandbox |
