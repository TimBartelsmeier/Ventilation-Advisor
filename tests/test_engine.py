"""Tests for v3 comfort goals, live protections and persisted opening lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.ventilation_advisor.const import (
    Reason,
    Recommendation,
    Resolution,
    ScenarioStatus,
    WindowState,
)
from custom_components.ventilation_advisor.engine import (
    Inputs,
    Reading,
    RoomState,
    ThermostatReading,
    evaluate,
)
from custom_components.ventilation_advisor.physics import (
    absolute_humidity,
    relative_humidity,
)
from custom_components.ventilation_advisor.settings import defaults

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
CONTACT = "binary_sensor.window"
OPEN = (
    Reason.DRYING_POSSIBLE,
    Reason.COOLING_POSSIBLE,
    Reason.WARMING_POSSIBLE,
    Reason.HUMIDIFYING_POSSIBLE,
    Reason.OVERDUE_AIRING,
)
PROTECT = (
    Reason.HEAT_LOSS,
    Reason.HEAT_GAIN,
    Reason.MOISTURE_LOSS,
    Reason.MOISTURE_GAIN,
)


def minute(value: float) -> datetime:
    return T0 + timedelta(minutes=value)


class Room:
    """Mutable driver around the pure evaluator."""

    def __init__(self, **overrides: Any) -> None:
        self.settings = defaults() | overrides
        self.state = RoomState(history_baseline_at=T0 - timedelta(hours=1))
        self.contacts: dict[str, bool | None] = {CONTACT: False}
        self.changed_at: datetime | None = None
        self.it: float | str | None = 21
        self.ih: float | str | None = 50
        self.ot: float | str | None = 5
        self.oh: float | str | None = 80
        self.thermostat: ThermostatReading | None = None
        self.paused = False

    @staticmethod
    def _reading(entity: str, value: float | str | None) -> Reading | None:
        if value is None:
            return None
        return (
            Reading(entity, error=value)
            if isinstance(value, str)
            else Reading(entity, value)
        )

    def eval(self, at: datetime):
        return evaluate(
            self.state,
            Inputs(
                contacts=dict(self.contacts),
                contacts_changed_at=self.changed_at,
                indoor_temperature=self._reading("sensor.it", self.it),
                indoor_humidity=self._reading("sensor.ih", self.ih),
                outdoor_temperature=self._reading("sensor.ot", self.ot),
                outdoor_humidity=self._reading("sensor.oh", self.oh),
                thermostat=self.thermostat,
                advisor_enabled=not self.paused,
            ),
            self.settings,
            at,
        )

    def window(self, opened: bool | None, at: datetime) -> None:
        self.contacts[CONTACT], self.changed_at = opened, at

    def open(self, at: float = 6, confirm: float = 7):
        self.window(True, minute(at))
        return self.eval(minute(confirm))

    def close(self, at: float, confirm: float | None = None):
        self.window(False, minute(at))
        return self.eval(minute(confirm if confirm is not None else at + 0.5))

    def outdoor_reference_rh(self, humidity: float) -> None:
        reference = (
            max(self.it, self.thermostat.target)
            if self.thermostat and self.thermostat.heating
            else self.it
        )
        self.oh = relative_humidity(self.ot, absolute_humidity(reference, humidity))


def active_room(reason: Reason, **settings) -> Room:
    room = Room(**settings)
    if reason is Reason.DRYING_POSSIBLE:
        room.ih = 70
    elif reason is Reason.COOLING_POSSIBLE:
        room.it, room.ot = 26, 18
    elif reason is Reason.WARMING_POSSIBLE:
        room.it, room.ot = 17.5, 22
    elif reason is Reason.HUMIDIFYING_POSSIBLE:
        room.ih, room.ot = 30, 21
        room.outdoor_reference_rh(60)
    elif reason is Reason.OVERDUE_AIRING:
        room.state.history_baseline_at = T0 - timedelta(hours=49)
    room.eval(T0)
    room.eval(minute(5))
    return room


@pytest.mark.parametrize("reason", OPEN)
def test_all_open_scenarios_share_condition_delay(reason) -> None:
    room = active_room(reason)
    latch = room.state.latch(reason)
    assert latch.active
    assert latch.last_active_at == minute(5)
    result = room.eval(minute(5))
    assert result.recommendation is Recommendation.OPEN
    assert reason in result.reason_ids
    assert all(info.status == "active" for info in result.reasons)


def test_drying_settles_and_releases_with_global_delays() -> None:
    room = Room()
    room.ih = 70
    assert room.eval(T0).next_deadline == minute(5)
    assert room.eval(minute(5)).recommendation is Recommendation.OPEN
    room.ih = 52
    result = room.eval(minute(6))
    assert result.scenarios[Reason.DRYING_POSSIBLE] is ScenarioStatus.RELEASING
    assert result.recommendation is Recommendation.OPEN
    assert (
        room.eval(minute(7)).scenarios[Reason.DRYING_POSSIBLE]
        is ScenarioStatus.RESOLVED
    )
    assert (
        room.state.drying_possible.resolution_code
        == Resolution.DRYING_POSSIBLE_TARGET_REACHED
    )


@pytest.mark.parametrize(
    "reason,indoor,outdoor",
    [(Reason.COOLING_POSSIBLE, 26, 5), (Reason.WARMING_POSSIBLE, 17, 35)],
)
def test_helpful_pull_has_no_do_no_harm_gate(reason, indoor, outdoor) -> None:
    room = Room(condition_delay=0)
    room.it, room.ot = indoor, outdoor
    room.ih = 70 if reason is Reason.WARMING_POSSIBLE else 30
    assert reason in room.eval(T0).reason_ids


def test_drying_in_already_cold_room_is_independent_of_temperature() -> None:
    room = Room(condition_delay=0)
    room.it, room.ot, room.ih, room.oh = 17, 0, 70, 80
    result = room.eval(T0)
    assert result.recommendation is Recommendation.OPEN
    assert result.reason_ids == [Reason.DRYING_POSSIBLE]


@pytest.mark.parametrize(
    "indoor,previous,expected",
    [
        (23.5, "within", "within"),
        (23.51, "within", "above"),
        (22.6, "above", "above"),
        (22.5, "above", "within"),
        (18.49, "within", "below"),
        (19.4, "below", "below"),
        (19.5, "below", "within"),
    ],
)
def test_temperature_hysteresis_is_shared(indoor, previous, expected) -> None:
    room = Room()
    room.state.temperature_zone, room.it = previous, indoor
    room.eval(T0)
    assert room.state.temperature_zone == expected


def test_meaningful_helpful_difference_hysteresis() -> None:
    room = Room(condition_delay=0, goal_reached_delay=0)
    room.it, room.ot = 26, 24.1
    assert Reason.COOLING_POSSIBLE not in room.eval(T0).reason_ids
    room.ot = 24
    assert Reason.COOLING_POSSIBLE in room.eval(minute(1)).reason_ids
    room.ot = 25
    assert (
        room.eval(minute(2)).scenarios[Reason.COOLING_POSSIBLE] is ScenarioStatus.ACTIVE
    )
    room.ot = 25.01
    room.eval(minute(3))
    assert (
        room.state.cooling_possible.resolution_code
        == Resolution.COOLING_POSSIBLE_NO_LONGER_HELPFUL
    )


@pytest.mark.parametrize(
    "setpoint,heating,lower,upper,reference",
    [
        (21, True, 21, 23, 21),
        (17, True, 19, 23, 20),
        (24, True, 24, 25, 24),
        (23, True, 23, 24, 23),
        (30, False, 19, 23, 20),
    ],
)
def test_effective_comfort_window_and_reference(
    setpoint, heating, lower, upper, reference
) -> None:
    room = Room()
    room.it = 20
    room.thermostat = ThermostatReading(
        "climate.room", heating=heating, target=setpoint
    )
    result = room.eval(T0)
    assert result.values["comfort_lower_temperature"] == lower
    assert result.values["comfort_upper_temperature"] == upper
    assert result.values["humidity_reference_temperature"] == reference
    assert result.values["indoor_temperature"] == 20
    assert result.values["outdoor_temperature"] == 5
    if heating:
        assert result.values["heating_setpoint"] == setpoint
    else:
        assert "heating_setpoint" not in result.values


def test_high_setpoint_avoids_cooling_and_uses_quarter_degree_margin() -> None:
    room = Room(condition_delay=0)
    room.thermostat = ThermostatReading("climate.room", True, 24)
    room.it, room.ot = 24.5, 15
    assert Reason.COOLING_POSSIBLE not in room.eval(T0).reason_ids
    room.open()
    room.it = 24.26
    assert room.eval(minute(30)).recommendation is Recommendation.NONE
    room.it = 24.25
    assert room.eval(minute(31)).reason_ids == [Reason.HEAT_LOSS]


def test_humidity_uses_both_water_contents_at_max_actual_and_setpoint() -> None:
    room = Room(condition_delay=0)
    room.it, room.ih, room.ot, room.oh = 16, 70, 0, 80
    room.thermostat = ThermostatReading("climate.room", True, 21)
    result = room.eval(T0)
    assert result.values["indoor_reference_humidity"] == pytest.approx(
        relative_humidity(21, absolute_humidity(16, 70))
    )
    assert result.values["outdoor_reference_humidity"] == pytest.approx(
        relative_humidity(21, absolute_humidity(0, 80))
    )
    assert Reason.DRYING_POSSIBLE not in result.reason_ids
    room.it = 25
    assert room.eval(minute(1)).values["humidity_reference_temperature"] == 25
    room.thermostat.heating = False
    room.it = 16
    assert Reason.DRYING_POSSIBLE in room.eval(minute(2)).reason_ids


def test_temperature_missing_uses_longest_without_setpoint_fallback() -> None:
    room = Room()
    room.it = "unavailable"
    room.thermostat = ThermostatReading("climate.room", True, 24)
    room.eval(T0)
    result = room.open()
    assert result.recommended_duration_s == 45 * 60
    assert result.maximum_duration_s == 67.5 * 60
    assert result.indoor_reference_temperature is None
    room.it = 24
    recovered = room.eval(minute(8))
    assert recovered.recommended_duration_s == 45 * 60
    assert recovered.maximum_duration_s == 67.5 * 60


@pytest.mark.parametrize("indoor,outdoor", [(21, 11), (11, 21), (21, 21)])
def test_both_durations_frozen_on_confirmation_actual_difference(
    indoor, outdoor
) -> None:
    room = Room()
    room.it, room.ot = indoor, outdoor
    room.eval(T0)
    room.window(True, minute(1))
    room.eval(minute(1))
    room.it, room.ot = indoor + 2, outdoor + 2
    result = room.eval(minute(2))
    expected = 10 * 60 if indoor != outdoor else 45 * 60
    assert result.recommended_duration_s == expected
    assert result.maximum_duration_s == expected * 1.5
    room.it, room.ot = 19, -10
    room.settings["reference_airing_time"] = 20
    result = room.eval(minute(3))
    assert result.recommended_duration_s == expected
    assert result.maximum_duration_s == expected * 1.5


@pytest.mark.parametrize(
    "reason,indoor,outdoor,humidity,outdoor_humidity",
    [
        (Reason.HEAT_LOSS, 19.5, 9.5, 50, 50),
        (Reason.HEAT_GAIN, 22.5, 32.5, 50, 50),
        (Reason.MOISTURE_LOSS, 21, 11, 43, 20),
        (Reason.MOISTURE_GAIN, 21, 31, 52, 75),
    ],
)
def test_all_protections_only_after_strict_maximum_and_near_edge(
    reason, indoor, outdoor, humidity, outdoor_humidity
) -> None:
    room = Room(moisture_loss_notification_enabled=True)
    room.it, room.ot, room.ih = indoor, outdoor, humidity
    room.outdoor_reference_rh(outdoor_humidity)
    room.eval(T0)
    room.open(at=1, confirm=2)
    maximum_at = minute(16)
    at_boundary = room.eval(maximum_at)
    assert at_boundary.recommendation is Recommendation.NONE
    assert at_boundary.next_deadline == maximum_at + timedelta(microseconds=1)
    result = room.eval(maximum_at + timedelta(microseconds=1))
    assert result.recommendation is Recommendation.CLOSE
    assert reason in result.reason_ids
    info = next(info for info in result.reasons if info.reason == reason)
    assert info.status == ScenarioStatus.ACTIVE
    assert info.sample["maximum_duration_s"] == 900
    assert info.sample["open_for_s"] > 900
    assert "comfort_lower_temperature" in info.sample


@pytest.mark.parametrize(
    "reason,indoor,outdoor,humidity,outdoor_humidity",
    [
        (Reason.HEAT_LOSS, 21, 5, 50, 50),
        (Reason.HEAT_GAIN, 21, 35, 50, 50),
        (Reason.MOISTURE_LOSS, 21, 5, 50, 10),
        (Reason.MOISTURE_GAIN, 21, 35, 47, 90),
    ],
)
def test_protections_do_not_warn_while_room_is_inside_window(
    reason, indoor, outdoor, humidity, outdoor_humidity
) -> None:
    room = Room(moisture_loss_notification_enabled=True)
    room.it, room.ot, room.ih = indoor, outdoor, humidity
    room.outdoor_reference_rh(outdoor_humidity)
    room.eval(T0)
    room.open()
    assert reason not in room.eval(minute(60)).reason_ids


def test_thermal_protection_release_hysteresis_is_live() -> None:
    room = Room()
    room.it = 19.5
    room.eval(T0)
    room.open()
    assert Reason.HEAT_LOSS in room.eval(minute(30)).reason_ids
    room.it = 19.8
    assert Reason.HEAT_LOSS in room.eval(minute(31)).reason_ids
    room.it = 20
    assert Reason.HEAT_LOSS in room.eval(minute(32)).reason_ids
    room.it = 20.01
    assert Reason.HEAT_LOSS not in room.eval(minute(33)).reason_ids
    room.it = 19.8
    assert Reason.HEAT_LOSS not in room.eval(minute(34)).reason_ids
    room.it = 19.5
    assert Reason.HEAT_LOSS in room.eval(minute(35)).reason_ids
    room.ot = 19.6
    assert Reason.HEAT_LOSS not in room.eval(minute(36)).reason_ids


def test_manual_cold_room_is_protected_even_without_goals() -> None:
    room = Room()
    room.it, room.ot = 17, 5
    room.eval(T0)
    assert room.open().recommendation is Recommendation.NONE
    assert room.state.session.reasons == {}
    assert room.eval(minute(30)).reason_ids == [Reason.HEAT_LOSS]


def test_moisture_loss_is_only_default_disabled_scenario() -> None:
    settings = defaults()
    for reason in (*OPEN, *PROTECT):
        assert settings[f"{reason}_notification_enabled"] is (
            reason is not Reason.MOISTURE_LOSS
        )


@pytest.mark.parametrize("reason", OPEN)
def test_all_open_and_goal_toggles(reason) -> None:
    room = active_room(reason, goal_reached_delay=0)
    room.settings[f"{reason}_goal_reached_notification_enabled"] = False
    room.open()
    room.it, room.ih = 21, 50
    result = room.eval(minute(20))
    assert all(info.reason != reason for info in result.reasons)
    room.settings[f"{reason}_goal_reached_notification_enabled"] = True
    result = room.eval(minute(21))
    assert result.recommendation is Recommendation.CLOSE
    assert reason in result.reason_ids
    room.settings[f"{reason}_notification_enabled"] = False
    room.eval(minute(22))
    assert reason not in room.state.session.reasons
    room.settings[f"{reason}_notification_enabled"] = True
    room.eval(minute(23))
    assert reason not in room.state.session.reasons


@pytest.mark.parametrize("reason", PROTECT)
def test_every_protection_toggle_disables_its_latch(reason) -> None:
    room = Room(**{f"{reason}_notification_enabled": False})
    room.eval(T0)
    room.open()
    assert room.eval(minute(60)).scenarios[reason] is ScenarioStatus.DISABLED


@pytest.mark.parametrize(
    "reason,target",
    [
        (Reason.DRYING_POSSIBLE, 52),
        (Reason.COOLING_POSSIBLE, 22.5),
        (Reason.WARMING_POSSIBLE, 19.5),
        (Reason.HUMIDIFYING_POSSIBLE, 43),
    ],
)
def test_goal_reached_is_delayed_sticky_and_has_new_code(reason, target) -> None:
    room = active_room(reason)
    room.open()
    if reason in (Reason.DRYING_POSSIBLE, Reason.HUMIDIFYING_POSSIBLE):
        room.ih = target
    else:
        room.it = target
    assert room.eval(minute(8)).recommendation is Recommendation.NONE
    result = room.eval(minute(9))
    assert result.recommendation is Recommendation.CLOSE
    info = next(info for info in result.reasons if info.reason == reason)
    assert info.code == f"{reason}_target_reached"
    assert info.status == ScenarioStatus.RESOLVED
    frozen = dict(info.sample)
    room.it, room.ih = 26, 70
    result = room.eval(minute(10))
    assert result.recommendation is Recommendation.CLOSE
    assert (
        next(info for info in result.reasons if info.reason == reason).sample == frozen
    )


@pytest.mark.parametrize("reason", OPEN[:-1])
def test_goal_no_longer_helpful_code(reason) -> None:
    room = active_room(reason, goal_reached_delay=0)
    room.open()
    room.ot = room.it
    room.oh = room.ih
    result = room.eval(minute(8))
    assert result.recommendation is Recommendation.CLOSE
    assert (
        next(info for info in result.reasons if info.reason == reason).code
        == f"{reason}_no_longer_helpful"
    )


def test_all_enabled_goals_required_and_protections_do_not_resolve_them() -> None:
    room = active_room(Reason.COOLING_POSSIBLE, goal_reached_delay=0)
    room.ih, room.ot = 70, 5
    room.eval(minute(5))
    room.eval(minute(10))
    room.open(at=11, confirm=12)
    assert set(room.state.session.reasons) == {
        Reason.COOLING_POSSIBLE,
        Reason.DRYING_POSSIBLE,
    }
    room.it = 22.5
    assert room.eval(minute(13)).recommendation is Recommendation.NONE
    room.it = 19.5
    result = room.eval(minute(40))
    assert result.recommendation is Recommendation.CLOSE
    assert result.reason_ids == [Reason.HEAT_LOSS, Reason.COOLING_POSSIBLE]
    assert room.state.session.reasons[Reason.DRYING_POSSIBLE].resolution_code is None
    room.ih = 52
    assert Reason.DRYING_POSSIBLE in room.eval(minute(41)).reason_ids


def test_no_new_goals_admitted_after_confirmed_opening() -> None:
    room = Room(condition_delay=0)
    room.eval(T0)
    room.open()
    room.it, room.ot, room.ih = 26, 18, 70
    room.eval(minute(10))
    room.eval(minute(20))
    assert room.state.session.reasons == {}
    assert not room.state.cooling_possible.active
    assert not room.state.drying_possible.active


@pytest.mark.parametrize("offset,included", [(15, True), (15.01, False)])
def test_recently_active_goals_have_exact_fifteen_minute_lookback(
    offset, included
) -> None:
    room = active_room(Reason.DRYING_POSSIBLE, goal_reached_delay=0)
    room.ih = 52
    room.eval(minute(6))
    room.open(at=6 + offset, confirm=7 + offset)
    assert (Reason.DRYING_POSSIBLE in room.state.session.reasons) is included
    if included:
        part = room.state.session.reasons[Reason.DRYING_POSSIBLE]
        assert part.source == "active_recently"
        assert part.resolution_code == Resolution.DRYING_POSSIBLE_TARGET_REACHED


def test_goal_resolution_during_open_debounce_is_preserved() -> None:
    room = active_room(Reason.DRYING_POSSIBLE, goal_reached_delay=0)
    room.window(True, minute(6))
    room.eval(minute(6))
    room.ih = 52
    room.eval(minute(6.5))
    result = room.eval(minute(7))
    assert result.recommendation is Recommendation.CLOSE
    assert (
        room.state.session.reasons[Reason.DRYING_POSSIBLE].resolution_code
        == Resolution.DRYING_POSSIBLE_TARGET_REACHED
    )


@pytest.mark.parametrize("interval", [1, 48, 10_000])
def test_overdue_airing_interval_deadline(interval: float) -> None:
    room = Room(airing_interval=interval, condition_delay=0)
    due = room.state.history_baseline_at + timedelta(hours=interval)
    assert room.eval(due - timedelta(seconds=1)).recommendation is Recommendation.NONE
    result = room.eval(due)
    assert result.recommendation is Recommendation.OPEN
    assert any(reason.reason is Reason.OVERDUE_AIRING for reason in result.reasons)


def test_overdue_goal_uses_frozen_recommended_duration_and_qualifying_history() -> None:
    room = active_room(Reason.OVERDUE_AIRING, goal_reached_delay=0)
    opened = room.open()
    aired_at = minute(6) + timedelta(seconds=opened.recommended_duration_s)
    assert (
        room.eval(aired_at - timedelta(microseconds=1)).recommendation
        is Recommendation.NONE
    )
    result = room.eval(aired_at)
    assert result.recommendation is Recommendation.CLOSE
    assert result.reasons[0].code == Resolution.AIRED
    assert room.state.last_airing_at is None
    room.close(at=20)
    assert room.state.last_airing_at == minute(20)
    assert room.eval(minute(21)).next_airing_due_at == minute(20) + timedelta(hours=48)


def test_disabling_overdue_does_not_count_short_openings() -> None:
    room = Room(overdue_airing_notification_enabled=False)
    room.eval(T0)
    room.open(at=1, confirm=2)
    room.close(at=3)
    assert room.state.last_airing_at is None


def test_manual_long_opening_updates_history_even_if_all_scenarios_disabled() -> None:
    room = Room(
        **{f"{reason}_notification_enabled": False for reason in (*OPEN, *PROTECT)}
    )
    room.eval(T0)
    room.open()
    room.close(at=20)
    assert room.state.last_airing_at == minute(20)


def test_unavailable_resets_settle_release_but_keeps_active_open_advice() -> None:
    room = Room()
    room.ih = 70
    room.eval(T0)
    room.ih = "unavailable"
    assert (
        room.eval(minute(3)).scenarios[Reason.DRYING_POSSIBLE] is ScenarioStatus.UNKNOWN
    )
    assert room.state.drying_possible.state == "inactive"
    room.ih = 70
    assert room.eval(minute(4)).next_deadline == minute(9)
    room.eval(minute(9))
    room.ih = 52
    room.eval(minute(10))
    room.ih = "unavailable"
    result = room.eval(minute(11))
    assert room.state.drying_possible.state == "active"
    assert result.recommendation is Recommendation.OPEN
    assert result.reason_ids == [Reason.DRYING_POSSIBLE]
    assert result.reasons[0].sample["indoor_humidity"] == 52
    room.ih = 52
    assert room.eval(minute(12)).next_deadline == minute(13)


@pytest.mark.parametrize("missing", ["it", "ih", "ot", "oh", "thermostat"])
def test_unavailable_required_input_never_resolves_humidity_goal(missing) -> None:
    room = active_room(Reason.DRYING_POSSIBLE, goal_reached_delay=0)
    room.open()
    room.ih = 52
    if missing == "thermostat":
        room.thermostat = ThermostatReading("climate.room", error="unavailable")
    else:
        setattr(room, missing, "unavailable")
    room.eval(minute(30))
    assert room.state.session.reasons[Reason.DRYING_POSSIBLE].resolution_code is None
    assert room.state.drying_possible.active


def test_outage_freezes_only_dependent_scenarios() -> None:
    room = active_room(Reason.COOLING_POSSIBLE, goal_reached_delay=0)
    room.ih = 70
    room.eval(minute(5))
    room.eval(minute(10))
    room.open(at=11, confirm=12)
    room.ih, room.it = "unavailable", 22.5
    result = room.eval(minute(13))
    assert (
        room.state.session.reasons[Reason.COOLING_POSSIBLE].resolution_code
        == Resolution.COOLING_POSSIBLE_TARGET_REACHED
    )
    assert room.state.session.reasons[Reason.DRYING_POSSIBLE].resolution_code is None
    assert result.recommendation is Recommendation.NONE
    room.settings["drying_possible_goal_reached_notification_enabled"] = False
    assert room.eval(minute(14)).recommendation is Recommendation.CLOSE


def test_active_protection_freezes_with_sample_and_recovers_live() -> None:
    room = Room()
    room.it = 19.5
    room.eval(T0)
    room.open()
    first = room.eval(minute(30))
    assert first.reason_ids == [Reason.HEAT_LOSS]
    room.it = "unavailable"
    result = room.eval(minute(31))
    assert result.scenarios[Reason.HEAT_LOSS] is ScenarioStatus.UNKNOWN
    assert result.recommendation is Recommendation.CLOSE
    assert result.reasons[0].status == ScenarioStatus.ACTIVE
    assert result.reasons[0].sample == first.reasons[0].sample
    room.it = 21
    assert room.eval(minute(32)).recommendation is Recommendation.NONE


def test_missing_readings_cannot_start_protection_even_after_maximum() -> None:
    room = Room()
    room.eval(T0)
    room.open()
    room.it = "unavailable"
    assert room.eval(minute(60)).recommendation is Recommendation.NONE
    assert not room.state.heat_loss.active


def test_active_moisture_protection_freezes_on_humidity_outage() -> None:
    room = Room(moisture_loss_notification_enabled=True)
    room.ih = 43
    room.outdoor_reference_rh(20)
    room.eval(T0)
    room.open()
    first = room.eval(minute(30))
    assert first.reason_ids == [Reason.MOISTURE_LOSS]
    room.oh = "unavailable"
    result = room.eval(minute(31))
    assert result.recommendation is Recommendation.CLOSE
    assert result.reason_ids == [Reason.MOISTURE_LOSS]
    assert result.reasons[0].sample == first.reasons[0].sample


def test_goal_reached_close_advice_stays_during_subsequent_outage() -> None:
    room = active_room(Reason.DRYING_POSSIBLE, goal_reached_delay=0)
    room.open()
    room.ih = 52
    room.eval(minute(8))
    room.ih = "unavailable"
    result = room.eval(minute(9))
    assert result.recommendation is Recommendation.CLOSE
    assert result.reasons[0].code == Resolution.DRYING_POSSIBLE_TARGET_REACHED


def test_invalid_inputs_problem_delay_and_recovery() -> None:
    room = Room()
    room.ih = "unavailable"
    room.window(None, T0)
    result = room.eval(T0)
    assert result.problems == []
    assert {problem.role for problem in result.pending_problems} == {
        "contact",
        "indoor_humidity",
    }
    assert room.eval(minute(10)).problems
    room.ih = 50
    room.window(False, minute(11))
    assert room.eval(minute(11)).problems == []
    assert room.state.invalid_since == {}


def test_numerical_error_is_reported_without_crashing() -> None:
    room = Room()
    room.ot = -243.5
    result = room.eval(T0)
    assert "outdoor_humidity" in result.numeric_errors
    assert result.scenarios[Reason.DRYING_POSSIBLE] is ScenarioStatus.UNKNOWN


def test_contact_unknown_retains_each_contact_not_just_aggregate() -> None:
    room = Room()
    room.contacts["binary_sensor.other"] = False
    room.eval(T0)
    room.open()
    session_id = room.state.session.id
    room.window(None, minute(8))
    room.contacts["binary_sensor.other"] = False
    assert room.eval(minute(8)).window is WindowState.OPEN
    assert room.state.session.id == session_id
    room.contacts["binary_sensor.other"] = True
    room.eval(minute(9))
    room.window(False, minute(10))
    assert room.eval(minute(11)).window is WindowState.OPEN
    room.contacts["binary_sensor.other"] = None
    assert room.eval(minute(12)).window is WindowState.OPEN
    room.contacts["binary_sensor.other"], room.changed_at = False, minute(13)
    assert room.eval(minute(13.5)).window is WindowState.CLOSED


def test_debounce_unknown_does_not_change_confirmed_state() -> None:
    room = Room()
    room.eval(T0)
    room.window(True, minute(1))
    assert room.eval(minute(1)).window is WindowState.CLOSED
    room.window(None, minute(1.5))
    assert room.eval(minute(2)).window is WindowState.OPEN
    room.window(False, minute(3))
    assert room.eval(minute(3)).window is WindowState.OPEN
    room.window(None, minute(3.2))
    assert room.eval(minute(3.5)).window is WindowState.CLOSED


def test_close_bounce_preserves_same_session_and_raw_close_caps_elapsed() -> None:
    room = Room()
    room.it = 19.5
    room.eval(T0)
    room.open(at=1, confirm=2)
    identifier = room.state.session.id
    room.window(False, minute(5))
    assert room.eval(minute(5)).open_for_s == 240
    room.window(True, minute(5.2))
    assert room.eval(minute(5.2)).session_id == identifier
    assert room.eval(minute(5.2)).open_for_s == pytest.approx(252)


def test_unknown_initial_window_and_open_timestamp_after_restart_are_conservative() -> (
    None
):
    room = Room()
    room.window(None, T0)
    result = room.eval(T0)
    assert result.window is WindowState.UNKNOWN
    assert result.recommendation is None
    room.window(True, minute(-30))
    result = room.eval(minute(1))
    assert result.opened_at == minute(1)
    assert result.open_for_s == 0


def test_initial_on_contact_starts_one_opening_immediately() -> None:
    room = Room()
    room.window(True, minute(-30))
    result = room.eval(T0)
    assert result.window is WindowState.OPEN
    assert result.opened_at == T0
    assert result.open_for_s == 0
    identifier = result.session_id
    room.window(None, minute(1))
    result = room.eval(minute(30))
    assert result.window is WindowState.OPEN
    assert result.session_id == identifier
    assert room.state.session.last_reliable_open_at == T0


def test_state_roundtrip_preserves_every_latch_frozen_goal_and_duration() -> None:
    room = active_room(Reason.DRYING_POSSIBLE, goal_reached_delay=0)
    room.open()
    room.ih, room.it = 52, 19.5
    room.eval(minute(30))
    payload = room.state.to_dict()
    restored = RoomState.from_dict(payload)
    assert restored.to_dict() == payload
    room.state = restored
    result = room.eval(minute(31))
    assert result.recommendation is Recommendation.CLOSE
    assert result.reason_ids == [Reason.HEAT_LOSS, Reason.DRYING_POSSIBLE]


@pytest.mark.parametrize("last_seen,counts", [(8, False), (20, True)])
def test_restart_counts_only_reliably_observed_airing(last_seen, counts) -> None:
    room = Room()
    room.eval(T0)
    room.open()
    room.eval(minute(last_seen))
    room.window(None, minute(last_seen + 1))
    room.eval(minute(60))
    room.state = RoomState.from_dict(room.state.to_dict())
    room.state.finalize_after_restart()
    assert room.state.session is None
    assert room.state.window is None
    assert room.state.contact_states == {}
    assert room.state.last_airing_at == (minute(last_seen) if counts else None)
    assert all(not room.state.latch(reason).active for reason in PROTECT)


def test_pause_clears_conditions_and_goals_but_retains_lifecycle_and_history() -> None:
    room = active_room(Reason.DRYING_POSSIBLE)
    room.open()
    identifier = room.state.session.id
    room.paused = True
    result = room.eval(minute(10))
    assert result.recommendation is Recommendation.PAUSED
    assert result.session_id == identifier
    assert result.reasons == []
    assert room.state.session.reasons == {}
    assert all(
        result.scenarios[reason] is ScenarioStatus.DISABLED
        for reason in (*OPEN, *PROTECT)
    )
    room.close(at=20)
    assert room.state.last_airing_at == minute(20)
    room.paused = False
    assert room.eval(minute(21)).recommendation is Recommendation.NONE
