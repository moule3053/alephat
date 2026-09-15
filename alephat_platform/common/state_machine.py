"""Task/run transition helpers."""

from alephat_platform.common.enums import TERMINAL_TASK_STATUSES, TaskStatus
from alephat_platform.common.errors import ConflictError

# task_status -> allowed next statuses
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.SUCCEEDED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PARKED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PARKED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def assert_task_transition(current: str | TaskStatus, target: str | TaskStatus) -> None:
    cur = TaskStatus(current)
    tgt = TaskStatus(target)
    if cur == tgt:
        return
    allowed = TASK_TRANSITIONS.get(cur, frozenset())
    if tgt not in allowed:
        raise ConflictError(
            f"Illegal task transition {cur} -> {tgt}",
            error_code="illegal_transition",
        )


def is_terminal(status: str | TaskStatus) -> bool:
    return TaskStatus(status) in TERMINAL_TASK_STATUSES
