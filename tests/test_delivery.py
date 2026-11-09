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


def test_render_open_with_preview() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 8,
        "outdoor_humidity": 80,
        "comfort_upper_humidity": 55,
        "humidity_reference_temperature": 21,
    }
    ev.preview_duration_s = 720
    ev.preview_outdoor = 8.0
    title, body = messages.render(ev, room="Bathroom", language="en", to_local=_local)
    assert title == "Bathroom: open the window"
    assert "Indoors: 21.0 °C, 70.0 % RH" in body
    assert "Outdoors: 8.0 °C, 80.0 % RH (" in body
    assert "Opening a window might be beneficial to:\n• Reduce excess humidity." in body
    assert "Recommended for full air exchange: 12 min" in body
    assert "As of" not in body


def test_render_drying_partial_reason_sample_uses_current_readings() -> None:
    ev = _ev(
        Recommendation.OPEN,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE,
                "active",
                sample={"indoor_absolute_humidity": 14.1},
            )
        ],
    )
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 75,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
        "comfort_upper_humidity": 55,
        "humidity_reference_temperature": 21,
    }
    _, body = messages.render(ev, room="Bedroom", language="en", to_local=_local)
    assert body.splitlines()[0] == "Indoors: 21.0 °C, 75.0 % RH"
    assert "?" not in body


def test_render_close_heat_loss() -> None:
    ev = _ev(Recommendation.CLOSE, [ReasonInfo(Reason.HEAT_LOSS, "active")])
    ev.maximum_duration_s = 300
    ev.open_for_s = 360
    ev.values = {
        "indoor_temperature": 19.3,
        "outdoor_temperature": -3,
        "comfort_lower_temperature": 19,
    }
    title, body = messages.render(ev, room="Office", language="en", to_local=_local)
    assert title == "Office: close the window"
    assert "Window open: 6 min · Maximum airing time: 5 min" in body
    assert (
        "Window should be closed:\n• Further airing risks cooling below your comfort range."
        in body
    )
    assert messages.reminder_title("x", "en") == "Reminder: x"


def test_render_fractional_airing_time() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.OVERDUE_AIRING, "active")])
    ev.preview_duration_s = 22.5 * 60
    _, body = messages.render(ev, room="Study", language="en", to_local=_local)
    assert "Recommended for full air exchange: 22.5 min" in body


def test_render_german_notification() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 70,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
    }
    ev.preview_duration_s = 22.5 * 60
    title, body = messages.render(ev, room="Bad", language="de", to_local=_local)
    assert title == "Bad: Fenster öffnen"
    assert "Innen: 21,0 °C, 70,0 % rF" in body
    assert "Außen: 0,0 °C, 80,0 % rF (21,2 % bei Raumtemperatur)" in body
    assert "Fenster öffnen könnte helfen bei:\n• Feuchtigkeit senken." in body
    assert "Empfohlene Dauer für vollständigen Luftaustausch: 22,5 Min." in body


def test_render_german_heating_and_problem_labels() -> None:
    ev = _ev(Recommendation.CLOSE, [ReasonInfo(Reason.HEAT_LOSS, "active")])
    ev.values = {
        "indoor_temperature": 19,
        "outdoor_temperature": 0,
        "comfort_lower_temperature": 20,
        "heating_setpoint": 20,
    }
    title, body = messages.render(ev, room="Büro", language="de", to_local=_local)
    assert title == "Büro: Fenster schließen"
    assert "Heizungs-Solltemperatur" in body
    assert "Fenster sollte geschlossen werden:" in body
    problem = messages.Problem(
        "indoor_temperature",
        "sensor.office_temperature",
        "unavailable",
        T0,
    )
    title, body = messages.render_problems(
        [problem], room="Büro", language="de", to_local=_local, now=T0
    )
    assert title == "Büro: Problem mit Ventilation Advisor"
    assert (
        "Innentemperatur (sensor.office_temperature): unavailable seit 12:00." in body
    )


def test_render_close_uses_current_conditions_not_old_resolution_sample() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                Reason.DRYING_POSSIBLE,
                "resolved",
                "drying_possible_target_reached",
                {"indoor_temperature": 21.0},
            ),
        ],
    )
    _, body = messages.render(ev, room="Bath", language="en", to_local=_local)
    ev.values = {"indoor_temperature": 22, "outdoor_temperature": 18}
    _, body = messages.render(ev, room="Bath", language="en", to_local=_local)
    assert body.splitlines()[0] == "Indoors: 22.0 °C"
    assert "Window may be closed again:\n• Drying target reached." in body
    assert "aired" not in body


@pytest.mark.parametrize(
    ("indoor", "setpoint", "indoor_line", "outdoor_line"),
    [
        (
            21,
            None,
            "Indoors: 21.0 °C, 70.0 % RH",
            "Outdoors: 0.0 °C, 80.0 % RH (21.2 % at indoor temperature)",
        ),
        (
            17,
            21,
            "Indoors: 17.0 °C, 70.0 % RH (55.3 % at heating setpoint)",
            "Outdoors: 0.0 °C, 80.0 % RH (21.2 % at heating setpoint)",
        ),
        (
            21,
            19,
            "Indoors: 21.0 °C, 70.0 % RH",
            "Outdoors: 0.0 °C, 80.0 % RH (21.2 % at indoor temperature)",
        ),
    ],
)
def test_humidity_reference_integrated_into_conditions(
    indoor,
    setpoint,
    indoor_line,
    outdoor_line,
) -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    ev.values = {
        "indoor_temperature": indoor,
        "indoor_humidity": 70,
        "outdoor_temperature": 0,
        "outdoor_humidity": 80,
    }
    if setpoint is not None:
        ev.values["heating_setpoint"] = setpoint
    title, body = messages.render(ev, room="Bath", language="en", to_local=_local)
    assert body.splitlines()[:2] == [indoor_line, outdoor_line]
    assert title not in body
    assert body.count("Indoors:") == body.count("Outdoors:") == 1
    assert "As of" not in body


def test_equal_temperature_humidity_has_no_unnecessary_reference() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo("humidifying_possible", "active")])
    ev.values = {
        "indoor_temperature": 21,
        "indoor_humidity": 30,
        "outdoor_temperature": 21,
        "outdoor_humidity": 55,
    }
    _, body = messages.render(ev, room="Room", language="en", to_local=_local)
    assert body.splitlines()[:2] == [
        "Indoors: 21.0 °C, 30.0 % RH",
        "Outdoors: 21.0 °C, 55.0 % RH",
    ]
    assert "(" not in body


def test_temperature_only_message_omits_humidity_reference() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo("warming_possible", "active")])
    ev.values = {
        "indoor_temperature": 17,
        "indoor_humidity": 70,
        "outdoor_temperature": 22,
        "outdoor_humidity": 60,
        "heating_setpoint": 21,
        "comfort_lower_temperature": 21,
    }
    _, body = messages.render(ev, room="Room", language="en", to_local=_local)
    assert body.splitlines()[:2] == [
        "Indoors: 17.0 °C, 70.0 % RH",
        "Outdoors: 22.0 °C, 60.0 % RH",
    ]
    assert "• Warm the room toward your heating setpoint." in body
    assert "at heating setpoint)" not in body


def test_retained_conditions_during_sensor_outage() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                "heat_loss",
                "active",
                sample={
                    "indoor_temperature": 19,
                    "outdoor_temperature": 0,
                    "comfort_lower_temperature": 19,
                },
            ),
        ],
    )
    ev.values = {"indoor_temperature": 19.5}
    ev.maximum_duration_s, ev.open_for_s = 600, 660
    _, body = messages.render(ev, room="Room", language="en", to_local=_local)
    assert body.splitlines()[:2] == ["Indoors: 19.5 °C", "Outdoors: 0.0 °C"]
    assert "?" not in body


def test_releasing_keeps_open_fingerprint_and_does_not_resend() -> None:
    ev = _ev(Recommendation.OPEN, [ReasonInfo(Reason.DRYING_POSSIBLE, "active")])
    original = fingerprint(ev)
    state = TargetState()
    initial = msg()
    initial.fingerprint = original
    assert run(state, initial, Context(T0)) is ActionType.SEND
    ev.reasons[0].status = "releasing"
    assert fingerprint(ev) == original
    releasing = msg()
    releasing.fingerprint = fingerprint(ev)
    assert run(state, releasing, Context(m(1))) is None


def test_render_combined_close_advice_lists_overdue_resolution() -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(Reason.HEAT_LOSS, "active"),
            ReasonInfo(Reason.OVERDUE_AIRING, "resolved", "aired"),
            ReasonInfo(
                Reason.COOLING_POSSIBLE,
                "resolved",
                "cooling_possible_target_reached",
                {"indoor_temperature": 23.0},
            ),
        ],
    )
    ev.recommended_duration_s = 300
    ev.maximum_duration_s = 600
    ev.open_for_s = 660
    ev.values = {
        "indoor_temperature": 19.3,
        "outdoor_temperature": 0,
        "comfort_lower_temperature": 19,
    }
    title, body = messages.render(ev, room="Office", language="en", to_local=_local)
    assert title == "Office: close the window"
    assert body == (
        "Indoors: 19.3 °C\n"
        "Outdoors: 0.0 °C\n\n"
        "Window should be closed:\n"
        "• Further airing risks cooling below your comfort range.\n"
        "• Recommended fresh-air airing completed.\n"
        "• Cooling target reached.\n\n"
        "Window open: 11 min · Maximum airing time: 10 min"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "warming_possible",
        "cooling_possible",
        "drying_possible",
        "humidifying_possible",
        "heat_loss",
        "heat_gain",
        "moisture_loss",
        "moisture_gain",
    ],
)
def test_all_comfort_messages_render_numeric_context(reason: str) -> None:
    protective = reason in ("heat_loss", "heat_gain", "moisture_loss", "moisture_gain")
    ev = _ev(
        Recommendation.CLOSE if protective else Recommendation.OPEN,
        [ReasonInfo(reason, "active")],
    )
    ev.values = {
        "indoor_temperature": 21,
        "outdoor_temperature": 5,
        "comfort_lower_temperature": 21,
        "comfort_upper_temperature": 23,
        "comfort_lower_humidity": 40,
        "comfort_upper_humidity": 55,
        "humidity_reference_temperature": 21,
        "indoor_reference_humidity": 60,
        "outdoor_reference_humidity": 25,
        "heating_setpoint": 21,
    }
    ev.maximum_duration_s = 720
    ev.open_for_s = 900
    title, body = messages.render(ev, room="Room", language="en", to_local=_local)
    assert "?" not in body
    assert title == (
        "Room: close the window" if protective else "Room: open the window"
    )
    if reason in ("warming_possible", "heat_loss"):
        assert "heating setpoint" in body


@pytest.mark.parametrize(
    "reason",
    [
        "warming_possible",
        "cooling_possible",
        "drying_possible",
        "humidifying_possible",
    ],
)
@pytest.mark.parametrize("suffix", ["target_reached", "no_longer_helpful"])
def test_all_goal_completion_messages(reason: str, suffix: str) -> None:
    ev = _ev(
        Recommendation.CLOSE,
        [
            ReasonInfo(
                reason,
                "resolved",
                f"{reason}_{suffix}",
                {
                    "indoor_temperature": 21,
                    "outdoor_temperature": 21,
                    "humidity_reference_temperature": 21,
                },
            )
        ],
    )
    title, body = messages.render(ev, room="Room", language="en", to_local=_local)
    assert title == "Room: you can close the window"
    assert "?" not in body
