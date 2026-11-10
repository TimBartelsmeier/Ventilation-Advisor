"""Pure per-room evaluator for comfort goals and live airing protections."""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .const import (
    HYSTERESIS_K,
    HYSTERESIS_RH,
    Reason,
    Recommendation,
    ReferenceOrigin,
    Resolution,
    ScenarioStatus,
    WindowState,
)
from .physics import (
    absolute_humidity,
    adverse_effect,
    classify_quantity,
    comfort_margin,
    meaningful_pull,
    recommended_airing_duration_s,
    relative_humidity,
)

PARTICIPATION_LOOKBACK = timedelta(minutes=15)
_LOGGER = logging.getLogger(__name__)
OPEN_SCENARIOS = {
    Reason.DRYING_POSSIBLE: ("humidity", -1),
    Reason.COOLING_POSSIBLE: ("temperature", -1),
    Reason.WARMING_POSSIBLE: ("temperature", 1),
    Reason.HUMIDIFYING_POSSIBLE: ("humidity", 1),
    Reason.OVERDUE_AIRING: ("airing", 0),
}
PROTECTIONS = {
    Reason.HEAT_LOSS: ("temperature", -1),
    Reason.HEAT_GAIN: ("temperature", 1),
    Reason.MOISTURE_LOSS: ("humidity", -1),
    Reason.MOISTURE_GAIN: ("humidity", 1),
}


@dataclass(slots=True)
class Reading:
    """A normalised reading (temperatures in °C, humidity in %)."""

    entity_id: str
    value: float | None = None
    error: str | None = None

    @property
    def valid(self) -> bool:
        """Return whether the reading is usable."""
        return (
            self.error is None and self.value is not None and math.isfinite(self.value)
        )


@dataclass(slots=True)
class ThermostatReading:
    """Heating-only thermostat mode and target."""

    entity_id: str
    heating: bool = False
    target: float | None = None
    error: str | None = None


@dataclass(slots=True)
class Inputs:
    """All inputs for one evaluation; None readings are not configured."""

    contacts: dict[str, bool | None]
    contacts_changed_at: datetime | None = None
    indoor_temperature: Reading | None = None
    indoor_humidity: Reading | None = None
    outdoor_temperature: Reading | None = None
    outdoor_humidity: Reading | None = None
    thermostat: ThermostatReading | None = None
    advisor_enabled: bool = True


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _later(a: datetime | None, b: datetime) -> datetime:
    return max(a, b) if a is not None else b


@dataclass(slots=True)
class Latch:
    """A persisted condition, its delayed transitions and last valid sample."""

    state: str = "inactive"
    settling_since: datetime | None = None
    releasing_since: datetime | None = None
    last_active_at: datetime | None = None
    resolution_code: str | None = None
    resolution_at: datetime | None = None
    sample: dict[str, float] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Whether this condition is still active."""
        return self.state in ("active", "releasing")

    def reset(self) -> None:
        """Reset a condition without losing its activity/resolution history."""
        self.state = "inactive"
        self.settling_since = None
        self.releasing_since = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise."""
        data = asdict(self)
        for key in (
            "settling_since",
            "releasing_since",
            "last_active_at",
            "resolution_at",
        ):
            data[key] = _iso(getattr(self, key))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Latch:
        """Deserialise."""
        values = dict(data)
        for key in (
            "settling_since",
            "releasing_since",
            "last_active_at",
            "resolution_at",
        ):
            if key in values:
                values[key] = _dt(values[key])
        return cls(**values)


@dataclass(slots=True)
class Participation:
    """A frozen opening goal with a sticky resolution."""

    source: str
    last_active_at: datetime | None = None
    resolution_code: str | None = None
    resolved_at: datetime | None = None
    sample: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            **asdict(self),
            "last_active_at": _iso(self.last_active_at),
            "resolved_at": _iso(self.resolved_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Participation:
        """Deserialise."""
        return cls(
            source=data["source"],
            last_active_at=_dt(data.get("last_active_at")),
            resolution_code=data.get("resolution_code"),
            resolved_at=_dt(data.get("resolved_at")),
            sample=dict(data.get("sample") or {}),
        )


@dataclass(slots=True)
class Session:
    """One confirmed opening; both airing durations are immutable."""

    id: str
    opened_at: datetime
    confirmed_at: datetime
    reliable_open_since: datetime | None
    last_reliable_open_at: datetime
    recommended_duration_s: float
    maximum_duration_s: float
    sample: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, Participation] = field(default_factory=dict)
    excluded_reasons: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        """Serialise."""
        data = asdict(self)
        for key in (
            "opened_at",
            "confirmed_at",
            "reliable_open_since",
            "last_reliable_open_at",
        ):
            data[key] = _iso(getattr(self, key))
        data["reasons"] = {key: value.to_dict() for key, value in self.reasons.items()}
        data["excluded_reasons"] = sorted(self.excluded_reasons)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Session:
        """Deserialise."""
        values = dict(data)
        for key in (
            "opened_at",
            "confirmed_at",
            "reliable_open_since",
            "last_reliable_open_at",
        ):
            values[key] = _dt(values.get(key))
        values["reasons"] = {
            key: Participation.from_dict(value)
            for key, value in (data.get("reasons") or {}).items()
        }
        values["excluded_reasons"] = set(data.get("excluded_reasons") or ())
        return cls(**values)


@dataclass(slots=True)
class RoomState:
    """Persisted per-room debounce, history, conditions and opening state."""

    history_baseline_at: datetime
    last_airing_at: datetime | None = None
    window: str | None = None
    raw_window: str | None = None
    raw_since: datetime | None = None
    session: Session | None = None
    drying_possible: Latch = field(default_factory=Latch)
    cooling_possible: Latch = field(default_factory=Latch)
    warming_possible: Latch = field(default_factory=Latch)
    humidifying_possible: Latch = field(default_factory=Latch)
    overdue_airing: Latch = field(default_factory=Latch)
    heat_loss: Latch = field(default_factory=Latch)
    heat_gain: Latch = field(default_factory=Latch)
    moisture_loss: Latch = field(default_factory=Latch)
    moisture_gain: Latch = field(default_factory=Latch)
    temperature_zone: str = "within"
    humidity_zone: str = "within"
    contact_states: dict[str, bool] = field(default_factory=dict)
    pending_open_reasons: dict[str, dict[str, Any]] = field(default_factory=dict)
    invalid_since: dict[str, datetime] = field(default_factory=dict)

    def latch(self, reason: Reason | str) -> Latch:
        """Return a scenario's condition latch."""
        return getattr(self, str(reason))

    def to_dict(self) -> dict[str, Any]:
        """Serialise."""
        data = asdict(self)
        for key in ("history_baseline_at", "last_airing_at", "raw_since"):
            data[key] = _iso(getattr(self, key))
        data["session"] = self.session.to_dict() if self.session else None
        for reason in (*OPEN_SCENARIOS, *PROTECTIONS):
            data[reason] = self.latch(reason).to_dict()
        data["pending_open_reasons"] = {
            key: {
                **value,
                "last_active_at": _iso(value.get("last_active_at")),
                "resolution_at": _iso(value.get("resolution_at")),
            }
            for key, value in self.pending_open_reasons.items()
        }
        data["invalid_since"] = {
            key: _iso(value) for key, value in self.invalid_since.items()
        }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RoomState:
        """Deserialise."""
        values = dict(data)
        for key in ("history_baseline_at", "last_airing_at", "raw_since"):
            values[key] = _dt(values.get(key))
        if values["history_baseline_at"] is None:
            raise ValueError("missing history baseline")
        values["session"] = (
            Session.from_dict(data["session"]) if data.get("session") else None
        )
        for reason in (*OPEN_SCENARIOS, *PROTECTIONS):
            values[reason] = Latch.from_dict(data.get(reason) or {})
        values["pending_open_reasons"] = {
            key: {
                **value,
                "last_active_at": _dt(value.get("last_active_at")),
                "resolution_at": _dt(value.get("resolution_at")),
            }
            for key, value in (data.get("pending_open_reasons") or {}).items()
        }
        values["invalid_since"] = {
            key: parsed
            for key, raw in (data.get("invalid_since") or {}).items()
            if (parsed := _dt(raw)) is not None
        }
        return cls(**values)

    def finalize_after_restart(self) -> None:
        """End an interrupted opening using only the last reliable observation."""
        if self.session is not None:
            end = self.session.last_reliable_open_at
            if end >= self.session.opened_at + timedelta(
                seconds=self.session.recommended_duration_s
            ):
                self.last_airing_at = _later(self.last_airing_at, end)
                self.overdue_airing.reset()
                self.overdue_airing.resolution_code = Resolution.AIRED
                self.overdue_airing.resolution_at = end
            self.session = None
        self.window = self.raw_window = None
        self.raw_since = None
        self.contact_states.clear()
        self.pending_open_reasons.clear()
        for reason in OPEN_SCENARIOS:
            _freeze_latch(self.latch(reason))
        for reason in PROTECTIONS:
            self.latch(reason).reset()


@dataclass(slots=True)
class ReasonInfo:
    """An active protection or an opening goal participating in current advice."""

    reason: str
    status: str
    code: str | None = None
    sample: dict[str, float] = field(default_factory=dict)
    participation_source: str | None = None
    last_active_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True)
class Problem:
    """An invalid configured input."""

    role: str
    entity_id: str
    error: str
    since: datetime


@dataclass(slots=True)
class Evaluation:
    """Result of one evaluation."""

    evaluated_at: datetime
    recommendation: Recommendation | None
    window: WindowState
    reasons: list[ReasonInfo] = field(default_factory=list)
    scenarios: dict[str, str] = field(default_factory=dict)
    unknown_reason: str | None = None
    preview_duration_s: float | None = None
    preview_outdoor: float | None = None
    session_id: str | None = None
    opened_at: datetime | None = None
    last_airing_at: datetime | None = None
    next_airing_due_at: datetime | None = None
    problems: list[Problem] = field(default_factory=list)
    pending_problems: list[Problem] = field(default_factory=list)
    next_deadline: datetime | None = None
    values: dict[str, float] = field(default_factory=dict)
    recommended_duration_s: float | None = None
    maximum_duration_s: float | None = None
    open_for_s: float | None = None
    indoor_reference_temperature: float | None = None
    indoor_reference_origin: str | None = None
    temperature_difference: float | None = None
    drying_reference_temperature: float | None = None
    numeric_errors: dict[str, str] = field(default_factory=dict)

    @property
    def reason_ids(self) -> list[str]:
        """Return the reason identifiers of the advice."""
        return [reason.reason for reason in self.reasons]


class _Ctx:
    """Mutable bundle for one evaluation."""

    def __init__(
        self,
        state: RoomState,
        inputs: Inputs,
        settings: Mapping[str, Any],
        now: datetime,
    ):
        self.state, self.inputs, self.s, self.now = state, inputs, settings, now
        self.deadlines: list[datetime] = []
        self.numeric_errors: dict[str, tuple[str, str]] = {}
        self.started_session = False
        self.values: dict[str, float] = {}

    def deadline(self, when: datetime) -> None:
        if when > self.now:
            self.deadlines.append(when)


def _valid(reading: Reading | None) -> bool:
    return reading is not None and reading.valid


def _heating_valid(ctx: _Ctx) -> bool:
    thermostat = ctx.inputs.thermostat
    return thermostat is None or (
        thermostat.error is None
        and (
            not thermostat.heating
            or (thermostat.target is not None and math.isfinite(thermostat.target))
        )
    )


def _values(ctx: _Ctx) -> dict[str, float]:
    values = {}
    for role in (
        "indoor_temperature",
        "indoor_humidity",
        "outdoor_temperature",
        "outdoor_humidity",
    ):
        reading = getattr(ctx.inputs, role)
        if _valid(reading):
            values[role] = reading.value
    lower, upper = (
        ctx.s["comfort_lower_temperature"],
        ctx.s["comfort_upper_temperature"],
    )
    thermostat = ctx.inputs.thermostat
    if _heating_valid(ctx) and thermostat is not None and thermostat.heating:
        values["heating_setpoint"] = thermostat.target
        lower = max(lower, thermostat.target)
        upper = max(upper, lower + 1.0)
    values.update(
        comfort_lower_temperature=lower,
        comfort_upper_temperature=upper,
        comfort_lower_humidity=ctx.s["comfort_lower_humidity"],
        comfort_upper_humidity=ctx.s["comfort_upper_humidity"],
    )
    indoor = values.get("indoor_temperature")
    if indoor is not None and _heating_valid(ctx):
        reference = max(indoor, values.get("heating_setpoint", indoor))
        values["humidity_reference_temperature"] = reference
        for side in ("indoor", "outdoor"):
            temp, humidity = (
                values.get(f"{side}_temperature"),
                values.get(f"{side}_humidity"),
            )
            if temp is None or humidity is None:
                continue
            try:
                water_content = absolute_humidity(temp, humidity)
                values[f"{side}_absolute_humidity"] = water_content
                values[f"{side}_reference_humidity"] = (
                    humidity
                    if reference == temp
                    else relative_humidity(reference, water_content)
                )
            except (ValueError, ZeroDivisionError, OverflowError) as err:
                reading = getattr(ctx.inputs, f"{side}_humidity")
                ctx.numeric_errors[f"{side}_humidity"] = (
                    reading.entity_id,
                    f"numerical_error:{type(err).__name__}",
                )
    if "indoor_temperature" in values and "outdoor_temperature" in values:
        values["temperature_difference"] = (
            values["indoor_temperature"] - values["outdoor_temperature"]
        )
    return values


def _configured(ctx: _Ctx, quantity: str) -> bool:
    if quantity == "airing":
        return True
    roles = ("indoor_temperature", "outdoor_temperature")
    if quantity == "humidity":
        roles += ("indoor_humidity", "outdoor_humidity")
    return all(getattr(ctx.inputs, role) is not None for role in roles)


def _quantity(
    ctx: _Ctx, quantity: str
) -> tuple[float, float, float, float, float, float] | None:
    if not _heating_valid(ctx):
        return None
    suffix = "temperature" if quantity == "temperature" else "reference_humidity"
    indoor, outdoor = (
        ctx.values.get(f"indoor_{suffix}"),
        ctx.values.get(f"outdoor_{suffix}"),
    )
    if indoor is None or outdoor is None:
        return None
    lower, upper = (
        ctx.values[f"comfort_lower_{quantity}"],
        ctx.values[f"comfort_upper_{quantity}"],
    )
    margin = comfort_margin(
        lower, upper, HYSTERESIS_K if quantity == "temperature" else HYSTERESIS_RH
    )
    minimum = ctx.s[f"minimum_{quantity}_difference"]
    return indoor, outdoor, lower, upper, margin, minimum


def _enabled(ctx: _Ctx, reason: Reason | str) -> bool:
    return ctx.inputs.advisor_enabled and bool(ctx.s[f"{reason}_notification_enabled"])


def _duration(ctx: _Ctx) -> float:
    return recommended_airing_duration_s(
        ctx.values.get("temperature_difference"),
        ctx.s["reference_airing_time"],
        ctx.s["airing_time_lower_limit"],
        ctx.s["airing_time_upper_limit"],
    )


def _capture_opening_reasons(
    ctx: _Ctx, opened_at: datetime
) -> dict[str, dict[str, Any]]:
    candidates = {}
    for reason in OPEN_SCENARIOS:
        latch = ctx.state.latch(reason)
        if latch.active:
            source = "active_at_opening"
        elif (
            latch.last_active_at is not None
            and opened_at - PARTICIPATION_LOOKBACK <= latch.last_active_at <= opened_at
        ):
            source = "active_recently"
        else:
            continue
        candidates[reason] = {
            "source": source,
            "last_active_at": latch.last_active_at,
            "resolution_code": latch.resolution_code,
            "resolution_at": latch.resolution_at,
            "sample": dict(latch.sample),
        }
    return candidates


def _start_session(ctx: _Ctx, opened_at: datetime) -> None:
    recommended = _duration(ctx)
    session = Session(
        id=uuid.uuid4().hex,
        opened_at=opened_at,
        confirmed_at=ctx.now,
        reliable_open_since=opened_at,
        last_reliable_open_at=ctx.now,
        recommended_duration_s=recommended,
        maximum_duration_s=recommended * ctx.s["maximum_airing_time_factor"],
        sample=dict(ctx.values),
    )
    for reason, candidate in ctx.state.pending_open_reasons.items():
        if not _enabled(ctx, reason) or not _configured(
            ctx, OPEN_SCENARIOS[Reason(reason)][0]
        ):
            continue
        session.reasons[reason] = Participation(
            source=candidate["source"],
            last_active_at=candidate["last_active_at"],
            resolution_code=candidate["resolution_code"],
            resolved_at=candidate["resolution_at"],
            sample=candidate["sample"],
        )
    ctx.state.pending_open_reasons.clear()
    ctx.state.session = session
    ctx.started_session = True
    _LOGGER.debug(
        "Opening session=%s started; opened_at=%s confirmed_at=%s "
        "recommended_duration_s=%s maximum_duration_s=%s participating_goals=%s",
        session.id,
        opened_at,
        ctx.now,
        recommended,
        session.maximum_duration_s,
        list(session.reasons),
    )


def _end_session(ctx: _Ctx, session: Session, end: datetime) -> None:
    qualified = end >= session.opened_at + timedelta(
        seconds=session.recommended_duration_s
    )
    _LOGGER.debug(
        "Opening session=%s ended at %s; qualifies_as_airing=%s",
        session.id,
        end,
        qualified,
    )
    if qualified:
        ctx.state.last_airing_at = _later(ctx.state.last_airing_at, end)
        latch = ctx.state.overdue_airing
        latch.reset()
        latch.resolution_code, latch.resolution_at = Resolution.AIRED, end
    ctx.state.session = None
    for reason in PROTECTIONS:
        ctx.state.latch(reason).reset()


def _update_window(ctx: _Ctx) -> None:
    state, now = ctx.state, ctx.now
    state.contact_states = {
        key: value
        for key, value in state.contact_states.items()
        if key in ctx.inputs.contacts
    }
    for key, value in ctx.inputs.contacts.items():
        if value is not None:
            state.contact_states[key] = value
    if any(state.contact_states.values()):
        raw = WindowState.OPEN
    elif ctx.inputs.contacts and len(state.contact_states) == len(ctx.inputs.contacts):
        raw = WindowState.CLOSED
    else:
        raw = WindowState.UNKNOWN
    if raw != state.raw_window:
        changed = ctx.inputs.contacts_changed_at or now
        if state.raw_window == WindowState.UNKNOWN or (
            state.window is None and raw is WindowState.OPEN
        ):
            changed = now
        changed = min(changed, now)
        if state.raw_since is not None and changed < state.raw_since:
            changed = now
        state.raw_window, state.raw_since = raw, changed
        state.pending_open_reasons = (
            _capture_opening_reasons(ctx, changed) if raw is WindowState.OPEN else {}
        )
    if raw is WindowState.UNKNOWN:
        return
    assert state.raw_since is not None
    if state.window is None:
        state.window = raw
        if raw is WindowState.OPEN:
            _start_session(ctx, state.raw_since)
        return
    if raw == state.window:
        if raw is WindowState.OPEN:
            if state.session is None:
                _start_session(ctx, state.raw_since)
            elif any(value is True for value in ctx.inputs.contacts.values()):
                state.session.last_reliable_open_at = now
        return
    due = state.raw_since + timedelta(
        seconds=ctx.s[
            "window_open_debounce"
            if raw is WindowState.OPEN
            else "window_close_debounce"
        ]
    )
    if now < due:
        ctx.deadline(due)
        return
    if raw is WindowState.OPEN:
        state.window = raw
        _start_session(ctx, state.raw_since)
    else:
        if state.session is not None:
            _end_session(ctx, state.session, state.raw_since)
        state.window = raw


def _freeze_latch(latch: Latch) -> None:
    if latch.state == "settling":
        latch.reset()
    elif latch.state == "releasing":
        latch.state = "active"
        latch.releasing_since = None


def _update_latch(
    ctx: _Ctx,
    reason: Reason,
    configured: bool,
    valid: bool,
    starts: bool,
    resolution: str | None,
) -> ScenarioStatus:
    latch = ctx.state.latch(reason)
    if not _enabled(ctx, reason):
        latch.reset()
        return ScenarioStatus.DISABLED
    if not configured:
        latch.reset()
        return ScenarioStatus.NOT_CONFIGURED
    if not valid:
        _freeze_latch(latch)
        if latch.active:
            latch.last_active_at = ctx.now
        return ScenarioStatus.UNKNOWN
    if latch.active:
        latch.last_active_at = ctx.now
        if resolution is None:
            latch.state, latch.releasing_since = "active", None
            return ScenarioStatus.ACTIVE
        if latch.releasing_since is None:
            latch.state, latch.releasing_since = "releasing", ctx.now
        due = latch.releasing_since + timedelta(minutes=ctx.s["goal_reached_delay"])
        if ctx.now >= due:
            latch.reset()
            latch.resolution_code, latch.resolution_at = resolution, ctx.now
            return ScenarioStatus.RESOLVED
        ctx.deadline(due)
        return ScenarioStatus.RELEASING
    if not starts:
        latch.reset()
        return ScenarioStatus.INACTIVE
    if latch.state != "settling":
        latch.state, latch.settling_since = "settling", ctx.now
        latch.resolution_code, latch.resolution_at = None, None
    due = latch.settling_since + timedelta(minutes=ctx.s["condition_delay"])
    if ctx.now >= due:
        latch.state, latch.settling_since, latch.last_active_at = (
            "active",
            None,
            ctx.now,
        )
        return ScenarioStatus.ACTIVE
    ctx.deadline(due)
    return ScenarioStatus.SETTLING


def _open_scenario(
    ctx: _Ctx, reason: Reason, quantity: str, direction: int, window: WindowState
) -> ScenarioStatus:
    latch = ctx.state.latch(reason)
    data = _quantity(ctx, quantity)
    starts, resolution = False, None
    if data is not None:
        indoor, outdoor, lower, upper, margin, minimum = data
        suggestion_margin = ctx.s[
            f"{reason.removesuffix('_possible')}_suggestion_margin"
        ]
        threshold = (
            upper + suggestion_margin if direction < 0 else lower - suggestion_margin
        )
        starts = direction * (threshold - indoor) > 0 and meaningful_pull(
            indoor, outdoor, direction, minimum, False
        )
        reached = (
            indoor <= upper - margin if direction < 0 else indoor >= lower + margin
        )
        if reached:
            resolution = f"{reason}_target_reached"
        elif not meaningful_pull(indoor, outdoor, direction, minimum, True):
            resolution = f"{reason}_no_longer_helpful"
        if latch.active or starts:
            latch.sample = _sample(ctx)
    _LOGGER.debug(
        "Opening scenario=%s: quantity=%s values=%s zone=%s "
        "window=%s suggestion_threshold=%s starts=%s resolution=%s "
        "condition_delay_min=%s goal_delay_min=%s",
        reason,
        quantity,
        data,
        getattr(ctx.state, f"{quantity}_zone"),
        window,
        threshold if data is not None else None,
        starts,
        resolution,
        ctx.s["condition_delay"],
        ctx.s["goal_reached_delay"],
    )
    return _update_latch(
        ctx,
        reason,
        _configured(ctx, quantity),
        data is not None,
        starts and (window is WindowState.CLOSED or ctx.started_session),
        resolution,
    )


def _opening_end(ctx: _Ctx) -> datetime:
    end = ctx.now
    if ctx.state.raw_window == WindowState.CLOSED and ctx.state.raw_since is not None:
        end = ctx.state.raw_since
    return end


def _elapsed(ctx: _Ctx) -> float | None:
    session = ctx.state.session
    if session is None:
        return None
    end = _opening_end(ctx)
    return max(0.0, (end - session.opened_at).total_seconds())


def _sample(ctx: _Ctx) -> dict[str, float]:
    sample = dict(ctx.values)
    session = ctx.state.session
    if session is not None:
        sample.update(
            recommended_duration_s=session.recommended_duration_s,
            maximum_duration_s=session.maximum_duration_s,
            open_for_s=_elapsed(ctx),
        )
        for key in (
            "indoor_temperature",
            "outdoor_temperature",
            "temperature_difference",
        ):
            if key in session.sample:
                sample[f"opening_{key}"] = session.sample[key]
    return sample


def _overdue_airing(ctx: _Ctx, window: WindowState) -> tuple[ScenarioStatus, datetime]:
    baseline = ctx.state.last_airing_at or ctx.state.history_baseline_at
    due = baseline + timedelta(hours=ctx.s["airing_interval"])
    session = ctx.state.session
    aired = session is not None and _opening_end(ctx) >= session.opened_at + timedelta(
        seconds=session.recommended_duration_s
    )
    _LOGGER.debug(
        "Overdue airing: baseline=%s due=%s now=%s window=%s aired=%s",
        baseline,
        due,
        ctx.now,
        window,
        aired,
    )
    if session is not None and not aired:
        ctx.deadline(
            session.opened_at + timedelta(seconds=session.recommended_duration_s)
        )
    elif window is WindowState.CLOSED:
        ctx.deadline(due)
    latch = ctx.state.overdue_airing
    if latch.active or ctx.now >= due:
        latch.sample = _sample(ctx)
    return _update_latch(
        ctx,
        Reason.OVERDUE_AIRING,
        True,
        window is not WindowState.UNKNOWN,
        ctx.now >= due
        and (window is WindowState.CLOSED or ctx.started_session)
        and not aired,
        Resolution.AIRED if aired else None,
    ), due


def _protection(
    ctx: _Ctx, reason: Reason, quantity: str, direction: int
) -> ScenarioStatus:
    latch = ctx.state.latch(reason)
    if not _enabled(ctx, reason):
        _LOGGER.debug("Safeguard %s: disabled", reason)
        latch.reset()
        return ScenarioStatus.DISABLED
    if not _configured(ctx, quantity):
        _LOGGER.debug("Safeguard %s: required inputs not configured", reason)
        latch.reset()
        return ScenarioStatus.NOT_CONFIGURED
    session = ctx.state.session
    if session is None:
        _LOGGER.debug("Safeguard %s: no confirmed opening session", reason)
        latch.reset()
        return ScenarioStatus.INACTIVE
    maximum_at = session.opened_at + timedelta(seconds=session.maximum_duration_s)
    ctx.deadline(maximum_at + timedelta(microseconds=1))
    data = _quantity(ctx, quantity)
    if data is None:
        _LOGGER.debug(
            "Safeguard %s session=%s: invalid readings; evaluation frozen",
            reason,
            session.id,
        )
        return ScenarioStatus.UNKNOWN
    if _opening_end(ctx) <= maximum_at:
        _LOGGER.debug(
            "Safeguard %s session=%s: maximum airing time not exceeded; "
            "opening_end=%s maximum_at=%s",
            reason,
            session.id,
            _opening_end(ctx),
            maximum_at,
        )
        latch.reset()
        return ScenarioStatus.INACTIVE
    indoor, outdoor, lower, upper, margin, minimum = data
    bound = lower if direction < 0 else upper
    adverse = adverse_effect(
        indoor, outdoor, bound, direction, margin, minimum, latch.active
    )
    _LOGGER.debug(
        "Safeguard %s session=%s: adverse_effect=%s quantity=%s indoor=%s outdoor=%s "
        "bound=%s direction=%s margin=%s minimum_difference=%s previously_active=%s",
        reason,
        session.id,
        adverse,
        quantity,
        indoor,
        outdoor,
        bound,
        direction,
        margin,
        minimum,
        latch.active,
    )
    if adverse:
        latch.state, latch.last_active_at, latch.sample = (
            "active",
            ctx.now,
            _sample(ctx),
        )
        return ScenarioStatus.ACTIVE
    latch.reset()
    return ScenarioStatus.INACTIVE


def _update_participation(ctx: _Ctx) -> None:
    session = ctx.state.session
    if session is None:
        return
    if ctx.started_session:
        for reason in OPEN_SCENARIOS:
            latch = ctx.state.latch(reason)
            if latch.active and reason not in session.reasons and _enabled(ctx, reason):
                session.reasons[reason] = Participation(
                    "active_at_opening", latch.last_active_at, sample=dict(latch.sample)
                )
    for reason, part in list(session.reasons.items()):
        if not _enabled(ctx, reason) or not _configured(
            ctx, OPEN_SCENARIOS[Reason(reason)][0]
        ):
            del session.reasons[reason]
            session.excluded_reasons.add(reason)
            continue
        latch = ctx.state.latch(reason)
        if part.resolution_code is None and latch.resolution_code is not None:
            part.resolution_code, part.resolved_at = (
                latch.resolution_code,
                latch.resolution_at,
            )
            part.sample = dict(latch.sample)


def _arbitrate(ctx: _Ctx, result: Evaluation) -> None:
    if not ctx.inputs.advisor_enabled:
        result.recommendation = Recommendation.PAUSED
        return
    if result.window is WindowState.UNKNOWN:
        result.recommendation, result.unknown_reason = None, "window_unknown"
        return
    if result.window is WindowState.CLOSED:
        for reason in OPEN_SCENARIOS:
            latch = ctx.state.latch(reason)
            if _enabled(ctx, reason) and latch.active:
                result.reasons.append(
                    ReasonInfo(
                        reason,
                        latch.state,
                        sample=dict(latch.sample),
                        last_active_at=latch.last_active_at,
                    )
                )
        result.recommendation = (
            Recommendation.OPEN if result.reasons else Recommendation.NONE
        )
        return
    session = ctx.state.session
    if session is None:
        return
    for reason in PROTECTIONS:
        latch = ctx.state.latch(reason)
        if _enabled(ctx, reason) and latch.active:
            result.reasons.append(
                ReasonInfo(
                    reason,
                    ScenarioStatus.ACTIVE,
                    sample=dict(latch.sample),
                    last_active_at=latch.last_active_at,
                )
            )
    deciding = [
        (reason, part)
        for reason, part in session.reasons.items()
        if ctx.s[f"{reason}_goal_reached_notification_enabled"]
        and _enabled(ctx, reason)
    ]
    all_reached = bool(deciding) and all(
        part.resolution_code is not None for _, part in deciding
    )
    if result.reasons or all_reached:
        result.reasons.extend(
            ReasonInfo(
                reason,
                ScenarioStatus.RESOLVED,
                part.resolution_code,
                dict(part.sample),
                part.source,
                part.last_active_at,
                part.resolved_at,
            )
            for reason, part in deciding
            if part.resolution_code is not None
        )
        result.recommendation = Recommendation.CLOSE
    else:
        # Unreached goals are exposed for diagnostics, not actionable close advice.
        result.reasons = [
            ReasonInfo(
                reason,
                "unknown"
                if result.scenarios[reason] == ScenarioStatus.UNKNOWN
                else "active",
                sample=dict(part.sample),
                participation_source=part.source,
                last_active_at=part.last_active_at,
            )
            for reason, part in deciding
            if part.resolution_code is None
        ]


def _problems(ctx: _Ctx, result: Evaluation) -> None:
    current: dict[str, tuple[str, str, str]] = {}
    for entity, value in ctx.inputs.contacts.items():
        if value is None:
            current[f"contact:{entity}"] = ("contact", entity, "unavailable")
    needed = set()
    for reason, (quantity, _) in (*OPEN_SCENARIOS.items(), *PROTECTIONS.items()):
        if (
            _enabled(ctx, reason)
            and _configured(ctx, quantity)
            and quantity != "airing"
        ):
            needed.update(("indoor_temperature", "outdoor_temperature", "thermostat"))
            if quantity == "humidity":
                needed.update(("indoor_humidity", "outdoor_humidity"))
    for role in needed:
        reading = getattr(ctx.inputs, role)
        if reading is None:
            continue
        if role == "thermostat":
            valid, error = _heating_valid(ctx), reading.error or "invalid_target"
        else:
            valid, error = reading.valid, reading.error or "invalid"
        if not valid:
            current[f"{role}:{reading.entity_id}"] = (role, reading.entity_id, error)
        if role in ctx.numeric_errors:
            entity, error = ctx.numeric_errors[role]
            current[f"{role}:{entity}"] = (role, entity, error)
    for key in list(ctx.state.invalid_since):
        if key not in current:
            del ctx.state.invalid_since[key]
    delay = timedelta(minutes=ctx.s["problem_report_delay"])
    for key, (role, entity, error) in sorted(current.items()):
        since = ctx.state.invalid_since.setdefault(key, ctx.now)
        problem = Problem(role, entity, error, since)
        if ctx.now >= since + delay:
            result.problems.append(problem)
        else:
            result.pending_problems.append(problem)
            ctx.deadline(since + delay)


def evaluate(
    state: RoomState, inputs: Inputs, settings: Mapping[str, Any], now: datetime
) -> Evaluation:
    """Evaluate a room, preserving opening goals and unavailable conditions."""
    ctx = _Ctx(state, inputs, settings, now)
    ctx.values = _values(ctx)
    _update_window(ctx)
    window = WindowState(state.window) if state.window else WindowState.UNKNOWN
    for quantity in ("temperature", "humidity"):
        data = _quantity(ctx, quantity)
        if data is not None:
            indoor, _, lower, upper, margin, _ = data
            setattr(
                state,
                f"{quantity}_zone",
                classify_quantity(
                    indoor, lower, upper, margin, getattr(state, f"{quantity}_zone")
                ),
            )
    scenarios = {}
    for reason, (quantity, direction) in OPEN_SCENARIOS.items():
        if reason is not Reason.OVERDUE_AIRING:
            scenarios[reason] = _open_scenario(ctx, reason, quantity, direction, window)
    scenarios[Reason.OVERDUE_AIRING], due = _overdue_airing(ctx, window)
    _update_participation(ctx)
    for reason, (quantity, direction) in PROTECTIONS.items():
        scenarios[reason] = _protection(ctx, reason, quantity, direction)
    session = state.session
    reference = ctx.values.get("humidity_reference_temperature")
    result = Evaluation(
        evaluated_at=now,
        recommendation=Recommendation.NONE,
        window=window,
        scenarios=scenarios,
        values=ctx.values,
        session_id=session.id if session else None,
        opened_at=session.opened_at if session else None,
        last_airing_at=state.last_airing_at,
        next_airing_due_at=due,
        recommended_duration_s=session.recommended_duration_s if session else None,
        maximum_duration_s=session.maximum_duration_s if session else None,
        open_for_s=_elapsed(ctx),
        indoor_reference_temperature=ctx.values.get("indoor_temperature"),
        indoor_reference_origin=ReferenceOrigin.INDOOR
        if _valid(inputs.indoor_temperature)
        else None,
        temperature_difference=ctx.values.get("temperature_difference"),
        drying_reference_temperature=reference,
        numeric_errors={role: error for role, (_, error) in ctx.numeric_errors.items()},
    )
    _arbitrate(ctx, result)
    if result.recommendation is Recommendation.OPEN:
        result.preview_duration_s = _duration(ctx)
        result.preview_outdoor = ctx.values.get("outdoor_temperature")
    _problems(ctx, result)
    result.next_deadline = min(ctx.deadlines) if ctx.deadlines else None
    return result
