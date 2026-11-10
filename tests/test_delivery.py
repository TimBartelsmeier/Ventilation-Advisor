"""Tests for the delivery policy and message rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ventilation_advisor import messages
from custom_components.ventilation_advisor.const import (
    Reason,
    Recommendation,
    WindowState,
)
from custom_components.ventilation_advisor.delivery import (
    ActionType,
    Context,
    Message,
    TargetState,
    decide,
    fingerprint,
    record_failure,
    record_success,
    reminder_interval_s,
)
from custom_components.ventilation_advisor.engine import (
    Evaluation,
    ReasonInfo,
)
from custom_components.ventilation_advisor.settings import defaults

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def m(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def msg(
    kind: str = "open",
    fp: str = "a",
    *,
    session: str | None = None,
    body: str = "body",
    interval: float | None = 1800,
    presentation: str = "p",
) -> Message:
    return Message(
        kind=kind,
        session_id=session,
        fingerprint=f"{kind}|{session}|{fp}",
        title="t",
        body=body,
        presentation=presentation,
        reminder_interval_s=interval,
    )


def run(state: TargetState, desired, ctx: Context, *, persistent: bool = False):
    """Decide and record success; return the action type (or None)."""
    decision = decide(state, desired, ctx, persistent=persistent)
    if decision.action is not None:
        record_success(state, decision.action, ctx.now)
        return decision.action.type
    return None


# -- basic lifecycle -----------------------------------------------------------


def test_send_once_then_nothing_then_clear() -> None:
    state = TargetState()
    assert run(state, msg(), Context(T0)) is ActionType.SEND
    assert run(state, msg(body="new values"), Context(m(1))) is None
    assert run(state, None, Context(m(2))) is ActionType.CLEAR
    assert run(state, None, Context(m(3))) is None


def test_decision_reasons_explain_suppression_and_scheduling() -> None:
    state = TargetState()
    desired = msg()
    assert (
        decide(state, None, Context(T0), persistent=False).reason
        == "no_desired_message"
    )
    decision = decide(state, desired, Context(T0), persistent=False)
    assert decision.reason == "new_message"
    record_success(state, decision.action, T0)
    assert (
        decide(state, desired, Context(m(1)), persistent=False).reason
        == "waiting_for_reminder"
    )
    assert (
        decide(state, desired, Context(m(31)), persistent=False).reason
        == "reminder_due"
    )
    assert (
        decide(state, msg(interval=None), Context(m(1)), persistent=False).reason
        == "unchanged_reminders_disabled"
    )
    assert (
        decide(
            state,
            msg(fp="changed", interval=None),
            Context(m(1), update_on_reason_change=False),
            persistent=False,
        ).reason
        == "reason_update_disabled"
    )

    ctx = Context(m(2), quiet=True, quiet_end_at=m(60))
    decision = decide(state, desired, ctx, persistent=False)
    assert decision.reason == "quiet_hours_clear"
    record_success(state, decision.action, m(2))
    decision = decide(state, desired, ctx, persistent=False)
    assert decision.reason == "quiet_hours"
    assert decision.deadline == m(60)


def test_decision_reasons_explain_retry_budget() -> None:
    state = TargetState()
    desired = msg()
    action = decide(state, desired, Context(T0), persistent=False).action
    assert action is not None
    record_failure(state, action, T0, "test failure")
    decision = decide(state, desired, Context(T0), persistent=False)
    assert decision.reason == "retry_backoff"
    assert decision.action is None
    assert decision.deadline == state.retry_at
    state.retry_at = None
    decision = decide(state, desired, Context(T0), persistent=False)
    assert decision.reason == "retry_budget_exhausted"
    assert decision.action is None


def test_kind_change_sends() -> None:
    state = TargetState()
    run(state, msg("open"), Context(T0))
    assert run(state, msg("close", session="s"), Context(m(1))) is ActionType.SEND


@pytest.mark.parametrize(
    ("update", "expected"), [(True, ActionType.SEND), (False, None)]
)
def test_reason_change_for_notify_targets(update: bool, expected) -> None:
    state = TargetState()
    run(state, msg(fp="a"), Context(T0))
    ctx = Context(m(1), update_on_reason_change=update)
    assert run(state, msg(fp="b"), ctx) is expected
    assert state.fingerprint == msg(fp="b").fingerprint
    assert (
        run(state, msg(fp="b"), Context(m(2), update_on_reason_change=update)) is None
    )


def test_persistent_always_updates_on_reason_change() -> None:
    state = TargetState()
    run(state, msg(fp="a"), Context(T0), persistent=True)
    ctx = Context(m(1), update_on_reason_change=False)
    assert run(state, msg(fp="b"), ctx, persistent=True) is ActionType.SEND


def test_persistent_value_refresh_coalesced() -> None:
    state = TargetState()
    run(state, msg(), Context(T0), persistent=True)
    decision = decide(state, msg(body="v2"), Context(m(0.5)), persistent=True)
    assert decision.action is None
    assert decision.deadline == m(1)
    assert (
        run(state, msg(body="v2"), Context(m(1)), persistent=True) is ActionType.REFRESH
    )
    # Refresh does not postpone the reminder.
    assert state.last_semantic_at == T0


def test_presentation_change_resends() -> None:
    state = TargetState()
    run(state, msg(), Context(T0))
    assert run(state, msg(presentation="renamed"), Context(m(1))) is ActionType.SEND


# -- reminders -----------------------------------------------------------------


def test_reminder_after_interval() -> None:
    state = TargetState()
    run(state, msg(interval=600), Context(T0))
    decision = decide(state, msg(interval=600), Context(m(5)), persistent=False)
    assert decision.action is None and decision.deadline == m(10)
    assert run(state, msg(interval=600), Context(m(10))) is ActionType.REMINDER
    assert run(state, msg(interval=600), Context(m(15))) is None
    assert run(state, msg(interval=600), Context(m(20))) is ActionType.REMINDER


def test_no_reminder_without_interval() -> None:
    state = TargetState()
    run(state, msg(interval=None), Context(T0))
    assert run(state, msg(interval=None), Context(m(600))) is None


def test_reminder_base_after_quiet_end() -> None:
    state = TargetState()
    run(state, msg(interval=1800), Context(T0))
    ctx = Context(m(20), last_quiet_end=m(10))
    decision = decide(state, msg(interval=1800), ctx, persistent=False)
    assert decision.deadline == m(40)


# -- quiet hours ---------------------------------------------------------------


def test_quiet_suppresses_new_send_and_sends_at_end() -> None:
    state = TargetState()
    quiet = Context(T0, quiet=True, quiet_end_at=m(60))
    decision = decide(state, msg(), quiet, persistent=False)
    assert decision.action is None and decision.deadline == m(60)
    assert run(state, msg(), Context(m(60), last_quiet_end=m(60))) is ActionType.SEND


def test_quiet_does_not_queue_conditions_that_disappear() -> None:
    state = TargetState()
    assert run(state, msg(), Context(T0, quiet=True, quiet_end_at=m(60))) is None
    assert run(state, None, Context(m(30), quiet=True)) is None
    assert run(state, None, Context(m(60), last_quiet_end=m(60))) is None


def test_quiet_unchanged_message_not_resent_at_end() -> None:
    state = TargetState()
    run(state, msg(interval=None), Context(T0))
    assert run(state, msg(interval=None), Context(m(1), quiet=True)) is None
    assert run(state, msg(interval=None), Context(m(60), last_quiet_end=m(60))) is None


def test_quiet_clears_stale_message() -> None:
    state = TargetState()
    run(state, msg(fp="a"), Context(T0))
    quiet = Context(m(1), quiet=True, quiet_end_at=m(60))
    assert run(state, msg(fp="b"), quiet) is ActionType.CLEAR
    assert run(state, msg(fp="b"), Context(m(2), quiet=True)) is None
    assert run(state, msg(fp="b"), Context(m(60))) is ActionType.SEND


def test_quiet_clears_still_run() -> None:
    state = TargetState()
    run(state, msg(), Context(T0))
    assert run(state, None, Context(m(1), quiet=True)) is ActionType.CLEAR


def test_quiet_no_reminders() -> None:
    state = TargetState()
    run(state, msg(interval=60), Context(T0))
    assert run(state, msg(interval=60), Context(m(30), quiet=True)) is None


def test_protective_close_respects_quiet_hours() -> None:
    state = TargetState()
    hl = msg("close", session="s", interval=900)
    quiet = Context(T0, quiet=True, last_quiet_end=T0 - timedelta(hours=10))
    assert run(state, hl, quiet) is None
    assert run(state, hl, Context(m(60))) is ActionType.SEND
    assert run(state, hl, Context(m(75), quiet=True)) is None


def test_quiet_exempt_advice_keeps_normal_reminder_cadence() -> None:
    state = TargetState()
    desired = msg("close", interval=900)
    desired.quiet_exempt = True
    assert run(state, desired, Context(T0, quiet=True)) is ActionType.SEND
    assert run(state, desired, Context(m(15), quiet=True)) is ActionType.REMINDER
    end = Context(m(30), last_quiet_end=m(29))
    assert run(state, desired, end) is ActionType.REMINDER


# -- retries -------------------------------------------------------------------


def test_retry_backoff_and_exhaustion() -> None:
    state = TargetState()
    desired = msg(interval=None)
    now = T0
    for delay in (30, 120, 300):
        decision = decide(state, desired, Context(now), persistent=False)
        assert decision.action is not None
        record_failure(state, decision.action, now, "boom")
        blocked = decide(
            state, desired, Context(now + timedelta(seconds=1)), persistent=False
        )
        assert blocked.action is None
        assert blocked.deadline == now + timedelta(seconds=delay)
        now += timedelta(seconds=delay)
    decision = decide(state, desired, Context(now), persistent=False)
    record_failure(state, decision.action, now, "boom")
    assert (
        decide(
            state, desired, Context(now + timedelta(hours=1)), persistent=False
        ).action
        is None
    )
    # A semantic change gets a fresh budget.
    assert (
        decide(state, msg(fp="new"), Context(now), persistent=False).action is not None
    )


def test_success_resets_failures() -> None:
    state = TargetState()
    decision = decide(state, msg(), Context(T0), persistent=False)
    record_failure(state, decision.action, T0, "x")
    record_success(state, decision.action, m(1))
    assert state.failures == 0 and state.failed_key is None and state.displayed


def test_target_state_roundtrip() -> None:
    state = TargetState()
    run(state, msg(), Context(T0))
    restored = TargetState.from_dict(state.to_dict())
    assert restored.displayed and restored.fingerprint == state.fingerprint
    assert restored.last_semantic_at == T0


# -- reminder intervals --------------------------------------------------------


def _ev(rec: Recommendation, reasons) -> Evaluation:
    return Evaluation(
        evaluated_at=T0,
        recommendation=rec,
        window=WindowState.CLOSED,
        reasons=reasons,
    )


def test_reminder_intervals_per_recommendation() -> None:
    s = defaults()
    open_ev = _ev(
        Recommendation.OPEN,
        [
            ReasonInfo(Reason.OVERDUE_AIRING, "active"),
            ReasonInfo(Reason.DRYING_POSSIBLE, "active"),
        ],
    )
    assert reminder_interval_s(open_ev, s) == 120 * 60
    only_overdue = _ev(
        Recommendation.OPEN, [ReasonInfo(Reason.OVERDUE_AIRING, "active")]
    )
    assert reminder_interval_s(only_overdue, s) == 120 * 60
    assert (
        reminder_interval_s(
            only_overdue,
            s | {"airing_interval": 24, "overdue_airing_reminder_interval": 30},
        )
        == 30 * 60
    )
    close_ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(Reason.COOLING_POSSIBLE, "resolved", "x"),
        ],
    )
    assert (
        reminder_interval_s(
            close_ev, s | {"cooling_possible_goal_reached_reminder_interval": 45}
        )
        == 45 * 60
    )
    overdue_close = _ev(
        Recommendation.CLOSE, [ReasonInfo(Reason.OVERDUE_AIRING, "resolved", "aired")]
    )
    assert reminder_interval_s(overdue_close, s) is None
    assert (
        reminder_interval_s(
            overdue_close, s | {"overdue_airing_goal_reached_reminder_interval": 15}
        )
        == 15 * 60
    )
    hl = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo("heat_loss", "active"),
            ReasonInfo("drying_possible", "resolved", "drying_possible_target_reached"),
        ],
    )
    hl.maximum_duration_s = 20 * 60
    assert reminder_interval_s(hl, s) == 15 * 60
    assert (
        reminder_interval_s(
            hl,
            s
            | {
                "heat_loss_reminder_factor": 0,
                "drying_possible_goal_reached_reminder_interval": 0,
            },
        )
        is None
    )
    assert (
        reminder_interval_s(
            hl,
            s
            | {
                "heat_loss_reminder_factor": 1.5,
                "drying_possible_goal_reached_reminder_interval": 10,
            },
        )
        == 10 * 60
    )
    assert reminder_interval_s(open_ev, s | {"reminders_enabled": False}) is None
    zero = s | {
        "drying_possible_reminder_interval": 0,
        "overdue_airing_reminder_interval": 0,
    }
    assert reminder_interval_s(open_ev, zero) is None


@pytest.mark.parametrize(
    "reason", ["heat_loss", "heat_gain", "moisture_loss", "moisture_gain"]
)
@pytest.mark.parametrize("status", ["active", "releasing"])
@pytest.mark.parametrize(
    ("maximum", "factor", "minimum", "expected"),
    [(900, 0.75, 3, 675), (120, 0.75, 3, 180), (900, 0.5, 10, 600), (900, 0, 3, None)],
)
def test_safeguard_reminder_factor_and_minimum(
    reason, status, maximum, factor, minimum, expected
):
    ev = _ev(Recommendation.CLOSE, [ReasonInfo(reason, status)])
    ev.maximum_duration_s = maximum
    settings = defaults() | {
        f"{reason}_reminder_factor": factor,
        "minimum_safeguard_reminder_interval": minimum,
    }
    assert reminder_interval_s(ev, settings) == expected


def test_fingerprint_ignores_values() -> None:
    a = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    b = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    b.values = {"indoor_humidity": 99}
    assert fingerprint(a) == fingerprint(b)
    c = _ev(Recommendation.OPEN, [ReasonInfo(Reason.COOLING_POSSIBLE, "active")])
    assert fingerprint(a) != fingerprint(c)


# -- rendering -----------------------------------------------------------------


def _local(value: datetime) -> datetime:
    return value


def _render(ev: Evaluation, *, language: str = "en", room: str = "Room"):
    return messages.render(ev, room=room, language=language, to_local=_local)


def test_render_single_open_with_preview_exactly() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    ev.preview_duration_s = 720
    assert _render(ev) == (
        "Room is humid",
        "Outdoor air can dry the room; open a window.\nFull air exchange: about 12 min.\n\n"
        "Inside: 21 °C · 70% RH\nOutside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


def test_render_multiple_open_reasons_with_overdue_exactly() -> None:
    ev = _ev(
        Recommendation.OPEN,
        [
            ReasonInfo(Reason.DRYING_POSSIBLE, "active"),
            ReasonInfo(Reason.COOLING_POSSIBLE, "active"),
            ReasonInfo(Reason.OVERDUE_AIRING, "active"),
        ],
    )
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    ev.preview_duration_s = 1350
    assert _render(ev) == (
        "Room is humid and hot",
        "Outdoor air can dry and cool the room; open a window. Fresh air is also due.\n"
        "Full air exchange: about 22 min.\n\nInside: 21 °C · 70% RH\n"
        "Outside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


def test_render_overdue_only_exactly() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.OVERDUE_AIRING, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    assert _render(ev) == (
        "Room is due for airing",
        "It is time for fresh air; open a window.\n\nInside: 21 °C · 70% RH\n"
        "Outside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


def test_render_single_goal_reached_exactly() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            )
        ],
    )
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    assert _render(ev) == (
        "Room drying target reached",
        "Humidity is back in range; you can close the window.\n\nInside: 21 °C · 70% RH\n"
        "Outside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


def test_render_multiple_goals_reached_exactly() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            ),
            ReasonInfo(
                Reason.COOLING_POSSIBLE, "resolved", "cooling_possible_target_reached"
            ),
        ],
    )
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    assert _render(ev) == (
        "Room drying and cooling targets reached",
        "Humidity and temperature are back in range; you can close the window.\n\n"
        "Inside: 21 °C · 70% RH\nOutside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


@pytest.mark.parametrize(
    ("reason", "title", "line"),
    [
        (
            "drying_possible",
            "Room: outdoor air no longer helps drying",
            "Outdoor air no longer dries the room; you can close the window.",
        ),
        (
            "cooling_possible",
            "Room: outdoor air no longer helps cooling",
            "Outdoor air no longer cools the room; you can close the window.",
        ),
        (
            "warming_possible",
            "Room: outdoor air no longer helps warming",
            "Outdoor air no longer warms the room; you can close the window.",
        ),
        (
            "humidifying_possible",
            "Room: outdoor air no longer helps humidifying",
            "Outdoor air no longer humidifies the room; you can close the window.",
        ),
    ],
)
def test_render_no_longer_helpful_never_claims_goal_reached(
    reason, title, line
) -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [ReasonInfo(reason, "resolved", f"{reason}_no_longer_helpful")],
    )
    assert _render(ev) == (
        title,
        f"{line}\n\nInside: unavailable\nOutside: unavailable",
    )
    assert "target reached" not in title and "back in range" not in line


def test_render_mixed_goal_unhelpful_and_aired_resolutions_exactly() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            ),
            ReasonInfo(
                Reason.COOLING_POSSIBLE,
                "resolved",
                "cooling_possible_no_longer_helpful",
            ),
            ReasonInfo(Reason.OVERDUE_AIRING, "resolved", "aired"),
        ],
    )
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    assert _render(ev) == (
        "Room drying target reached",
        "Humidity is back in range.\nOutdoor air no longer cools the room.\n"
        "The recommended fresh-air duration has been reached; you can close the window.\n\n"
        "Inside: 21 °C · 70% RH\nOutside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )


@pytest.mark.parametrize(
    ("reason", "indoor", "lower", "title", "body"),
    [
        (
            Reason.HEAT_LOSS,
            19.5,
            True,
            "Room is getting cold",
            "Further airing risks cooling below your comfort range; close the window.",
        ),
        (
            Reason.HEAT_LOSS,
            18,
            True,
            "Room is cold",
            "Outdoor air cools the room further; close the window.",
        ),
        (
            Reason.HEAT_GAIN,
            23.5,
            False,
            "Room is getting hot",
            "Further airing risks warming above your comfort range; close the window.",
        ),
        (
            Reason.HEAT_GAIN,
            25,
            False,
            "Room is hot",
            "Outdoor air warms the room further; close the window.",
        ),
    ],
)
def test_render_heat_safeguard_near_and_beyond_exactly(
    reason, indoor, lower, title, body
) -> None:
    ev = _ev(Recommendation.CLOSE, [ReasonInfo(reason, "active")])
    bound = "comfort_lower_temperature" if lower else "comfort_upper_temperature"
    ev.values = {
        "indoor_temperature": indoor,
        "outdoor_temperature": 5,
        bound: 19 if lower else 24,
    }
    ev.open_for_s, ev.maximum_duration_s = 360, 600
    assert _render(ev) == (
        title,
        f"{body}\nOpen for 6 min · Airing limit about 10 min.\n\nInside: {indoor:g} °C\nOutside: 5 °C",
    )


def test_render_safeguard_with_resolved_goal_exactly() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(Reason.HEAT_LOSS, "active"),
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            ),
        ],
    )
    ev.values = {
        "indoor_temperature": 19.5,
        "indoor_humidity": 60,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "comfort_lower_temperature": 19,
    }
    ev.open_for_s, ev.maximum_duration_s = 360, 600
    assert _render(ev) == (
        "Room is getting cold",
        "Further airing risks cooling below your comfort range; close the window.\n"
        "The drying target has also been reached.\nOpen for 6 min · Airing limit about 10 min.\n\n"
        "Inside: 19.5 °C · 60% RH\nOutside: 0 °C · 80% RH (23% RH @ 19.5 °C)",
    )


def test_render_multiple_safeguards_with_resolved_goal_exactly() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(Reason.HEAT_LOSS, "active"),
            ReasonInfo(Reason.HEAT_GAIN, "active"),
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            ),
        ],
    )
    ev.values = {
        "indoor_temperature": 19,
        "indoor_humidity": 30,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "comfort_lower_temperature": 20,
        "comfort_upper_temperature": 23,
        "comfort_lower_humidity": 40,
    }
    ev.open_for_s, ev.maximum_duration_s = 120, 600
    assert _render(ev) == (
        "Room is cold and getting hot",
        "Avoid further cooling and warming; close the window.\n"
        "The drying target has also been reached.\nOpen for 2 min · Airing limit about 10 min.\n\n"
        "Inside: 19 °C · 30% RH\nOutside: 0 °C · 80% RH (24% RH @ 19 °C)",
    )


def test_render_heating_setpoint_relevant_and_irrelevant_exactly() -> None:
    relevant = _ev(Recommendation.OPEN, [ReasonInfo(Reason.WARMING_POSSIBLE, "active")])
    relevant.values = {
        "indoor_temperature": 17,
        "indoor_humidity": 50,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "heating_setpoint": 21,
        "comfort_lower_temperature": 21,
    }
    assert _render(relevant) == (
        "Room is cold",
        "Outdoor air can warm the room; open a window.\n\n"
        "Inside: 17 °C · 50% RH (heating setpoint 21 °C)\n"
        "Outside: 0 °C · 80% RH (21% RH @ 21 °C)",
    )
    irrelevant = _ev(
        Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")]
    )
    irrelevant.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "heating_setpoint": 19,
        "comfort_lower_temperature": 18,
    }
    assert _render(irrelevant) == (
        "Room is humid",
        "Outdoor air can dry the room; open a window.\n\nInside: 21 °C · 70% RH\n"
        "Outside: 0 °C · 80% RH (21% RH @ 21 °C)",
    )


def test_outdoor_equivalent_humidity_is_shown_for_thermal_only_advice() -> None:
    ev = _ev(Recommendation.CLOSE, [ReasonInfo(Reason.HEAT_LOSS, "active")])
    ev.values = {
        "indoor_temperature": 19,
        "indoor_humidity": 60,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "comfort_lower_temperature": 18,
    }
    ev.open_for_s, ev.maximum_duration_s = 60, 120
    assert _render(ev) == (
        "Room is getting cold",
        "Further airing risks cooling below your comfort range; close the window.\n"
        "Open for 1 min · Airing limit about 2 min.\n\nInside: 19 °C · 60% RH\n"
        "Outside: 0 °C · 80% RH (24% RH @ 19 °C)",
    )


def test_outdoor_equivalent_humidity_is_shown_for_overdue_advice() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.OVERDUE_AIRING, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 50,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
    }
    assert _render(ev) == (
        "Room is due for airing",
        "It is time for fresh air; open a window.\n\nInside: 21 °C · 50% RH\n"
        "Outside: 0 °C · 80% RH (21% RH @ 21 °C)",
    )


def test_outdoor_equivalent_humidity_is_shown_at_equal_temperatures() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.OVERDUE_AIRING, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 50,
        "outdoor_temperature": 21,
        "outdoor_humidity": 55,
    }
    assert _render(ev) == (
        "Room is due for airing",
        "It is time for fresh air; open a window.\n\nInside: 21 °C · 50% RH\n"
        "Outside: 21 °C · 55% RH (55% RH @ 21 °C)",
    )


def test_render_missing_and_partial_readings_exactly() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    assert _render(ev) == (
        "Room is humid",
        "Outdoor air can dry the room; open a window.\n\nInside: unavailable\nOutside: unavailable",
    )
    ev.values = {"indoor_temperature": 21, "outdoor_humidity": 80}
    assert _render(ev) == (
        "Room is humid",
        "Outdoor air can dry the room; open a window.\n\nInside: 21 °C\nOutside: 80% RH",
    )


def test_render_german_open_and_goal_exactly() -> None:
    opened = _ev(
        Recommendation.OPEN,
        [
            ReasonInfo(Reason.DRYING_POSSIBLE, "active"),
            ReasonInfo(Reason.OVERDUE_AIRING, "active"),
        ],
    )
    opened.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    opened.preview_duration_s = 720
    assert _render(opened, language="de", room="Bad") == (
        "Bad ist feucht",
        "Außenluft kann den Raum trocknen; Fenster öffnen. Frischluft ist ebenfalls fällig.\n"
        "Vollständiger Luftaustausch: etwa 12 Min.\n\nInnen: 21 °C · 70% rF\n"
        "Außen: 8 °C · 80% rF (36% rF @ 21 °C)",
    )
    goal = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE, "resolved", "drying_possible_target_reached"
            )
        ],
    )
    goal.values = opened.values
    assert _render(goal, language="de", room="Bad") == (
        "Bad: Ziel erreicht (Trocknung)",
        "Luftfeuchte ist wieder im Komfortbereich; Fenster kann geschlossen werden.\n\n"
        "Innen: 21 °C · 70% rF\nAußen: 8 °C · 80% rF (36% rF @ 21 °C)",
    )


def test_unsupported_language_falls_back_to_english_exactly() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
    }
    expected = (
        "Room is humid",
        "Outdoor air can dry the room; open a window.\n\nInside: 21 °C · 70% RH\n"
        "Outside: 8 °C · 80% RH (36% RH @ 21 °C)",
    )
    assert _render(ev) == expected
    assert _render(ev, language="unsupported") == expected


def test_render_german_problem_notification_exactly() -> None:
    problem = messages.Problem(
        "indoor_temperature", "sensor.office_temperature", "unavailable", T0
    )
    assert messages.render_problems(
        [problem], room="Büro", language="de", to_local=_local, now=T0
    ) == (
        "Büro: Problem mit Ventilation Advisor",
        "Innentemperatur (sensor.office_temperature): unavailable seit 12:00.\n"
        "Betroffene Szenarien behalten ihren letzten Zustand. Bei nicht verfügbaren Eingaben startet keine neue Empfehlung.",
    )
