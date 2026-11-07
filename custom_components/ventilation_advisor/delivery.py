"""Pure notification delivery policy (per room, per target)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .const import (
    CONTENT_REFRESH_MIN_S,
    RETRY_DELAYS_S,
    Recommendation,
)
from .engine import Evaluation


@dataclass(slots=True)
class Message:
    """Desired recommendation message for a room."""

    kind: str
    session_id: str | None
    fingerprint: str
    title: str
    body: str
    presentation: str
    reminder_interval_s: float | None
    quiet_exempt: bool = False

    @property
    def major(self) -> str:
        """Kind and session identity; a change always sends."""
        return f"{self.kind}|{self.session_id or ''}"


def fingerprint(evaluation: Evaluation) -> str:
    """Return the semantic identity of an evaluation's advice."""
    reasons = ",".join(
        sorted(
            f"{r.reason}:{'active' if r.status == 'releasing' else r.status}:{r.code or ''}"
            for r in evaluation.reasons
        )
    )
    rec = evaluation.recommendation
    return f"{rec}|{evaluation.session_id or ''}|{reasons}"


def reminder_interval_s(
    evaluation: Evaluation, settings: Mapping[str, Any]
) -> float | None:
    """Return the reminder interval for the advice, or ``None`` for none."""
    if not settings["reminders_enabled"]:
        return None
    minutes: list[float] = []
    if evaluation.recommendation in (Recommendation.OPEN, Recommendation.CLOSE):
        for reason in evaluation.reasons:
            suffix = (
                "goal_reached_reminder_interval"
                if reason.status == "resolved"
                else "reminder_interval"
            )
            minutes.append(settings[f"{reason.reason}_{suffix}"])
    positive = [value for value in minutes if value > 0]
    return min(positive) * 60.0 if positive else None


class ActionType(StrEnum):
    """Delivery action types."""

    SEND = "send"
    REMINDER = "reminder"
    REFRESH = "refresh"
    CLEAR = "clear"


@dataclass(slots=True)
class Action:
    """A delivery action to execute for one target."""

    type: ActionType
    message: Message | None = None

    @property
    def key(self) -> str:
        """Identity used for retry budgeting."""
        if self.message is None:
            return "clear"
        return f"send:{self.message.fingerprint}"


@dataclass(slots=True)
class TargetState:
    """Delivery state for one room/target pair."""

    displayed: bool = False
    major: str | None = None
    fingerprint: str | None = None
    content: str | None = None
    presentation: str | None = None
    last_semantic_at: datetime | None = None
    last_refresh_at: datetime | None = None
    failures: int = 0
    retry_at: datetime | None = None
    failed_key: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise the parts needed after a restart."""
        return {
            "displayed": self.displayed,
            "major": self.major,
            "fingerprint": self.fingerprint,
            "presentation": self.presentation,
            "last_semantic_at": (
                self.last_semantic_at.isoformat() if self.last_semantic_at else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TargetState:
        """Deserialise."""
        last = data.get("last_semantic_at")
        return cls(
            displayed=bool(data.get("displayed")),
            major=data.get("major"),
            fingerprint=data.get("fingerprint"),
            presentation=data.get("presentation"),
            last_semantic_at=datetime.fromisoformat(last) if last else None,
        )


@dataclass(slots=True)
class Context:
    """Time and settings context for a delivery decision."""

    now: datetime
    quiet: bool = False
    quiet_end_at: datetime | None = None
    last_quiet_end: datetime | None = None
    update_on_reason_change: bool = True


@dataclass(slots=True)
class Decision:
    """Result of ``decide``."""

    action: Action | None = None
    deadline: datetime | None = None


def _content(message: Message) -> str:
    return f"{message.title}\n{message.body}"


def decide(
    state: TargetState,
    desired: Message | None,
    ctx: Context,
    *,
    persistent: bool,
) -> Decision:
    """Decide the next action for one target. May update ``state`` silently."""
    action, deadline = _decide(state, desired, ctx, persistent)
    if action is not None and action.key == state.failed_key:
        if state.retry_at is None:
            return Decision(None, deadline)  # retry budget exhausted
        if ctx.now < state.retry_at:
            return Decision(None, _earliest(deadline, state.retry_at))
    return Decision(action, deadline)


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _decide(
    state: TargetState,
    desired: Message | None,
    ctx: Context,
    persistent: bool,
) -> tuple[Action | None, datetime | None]:
    if desired is None:
        if state.displayed:
            return Action(ActionType.CLEAR), None
        state.major = state.fingerprint = state.presentation = None
        return None, None

    major_changed = state.major != desired.major
    reason_changed = state.fingerprint != desired.fingerprint
    presentation_changed = state.presentation != desired.presentation
    sends_on_reason = persistent or ctx.update_on_reason_change

    if ctx.quiet and not desired.quiet_exempt:
        if state.displayed and (major_changed or (reason_changed and sends_on_reason)):
            # The displayed message became misleading: clear it now and let
            # quiet-end handling deliver the current advice.
            return Action(ActionType.CLEAR), None
        if state.displayed and reason_changed:
            state.fingerprint = desired.fingerprint
        return None, ctx.quiet_end_at

    if not state.displayed or major_changed or presentation_changed:
        return Action(ActionType.SEND, desired), None
    if reason_changed:
        if sends_on_reason:
            return Action(ActionType.SEND, desired), None
        state.fingerprint = desired.fingerprint

    deadline: datetime | None = None
    if desired.reminder_interval_s is not None and state.last_semantic_at:
        base = state.last_semantic_at
        if not desired.quiet_exempt and ctx.last_quiet_end is not None:
            base = max(base, ctx.last_quiet_end)
        due = base + timedelta(seconds=desired.reminder_interval_s)
        if ctx.now >= due:
            return Action(ActionType.REMINDER, desired), None
        deadline = due

    if persistent and state.content != _content(desired):
        refresh_at = (state.last_refresh_at or ctx.now) + timedelta(
            seconds=CONTENT_REFRESH_MIN_S
        )
        if state.last_refresh_at is None or ctx.now >= refresh_at:
            return Action(ActionType.REFRESH, desired), deadline
        deadline = _earliest(deadline, refresh_at)
    return None, deadline


def record_success(state: TargetState, action: Action, now: datetime) -> None:
    """Update ``state`` after a successful service call."""
    state.failures = 0
    state.retry_at = None
    state.failed_key = None
    state.last_error = None
    if action.type is ActionType.CLEAR or action.message is None:
        state.displayed = False
        state.major = state.fingerprint = state.content = state.presentation = None
        return
    message = action.message
    state.displayed = True
    state.major = message.major
    state.fingerprint = message.fingerprint
    state.presentation = message.presentation
    state.content = _content(message)
    state.last_refresh_at = now
    if action.type is not ActionType.REFRESH:
        state.last_semantic_at = now


def record_failure(
    state: TargetState, action: Action, now: datetime, error: str
) -> None:
    """Update ``state`` after a failed service call (bounded backoff)."""
    if state.failed_key != action.key:
        state.failures = 0
    state.failed_key = action.key
    state.failures += 1
    state.last_error = error
    if state.failures <= len(RETRY_DELAYS_S):
        state.retry_at = now + timedelta(seconds=RETRY_DELAYS_S[state.failures - 1])
    else:
        state.retry_at = None
