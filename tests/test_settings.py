"""Tests for the comfort-window settings model."""

from __future__ import annotations

import pytest

from custom_components.ventilation_advisor import settings as st
from custom_components.ventilation_advisor.settings import Kind, Scope


def test_keys_unique_and_defaults_valid() -> None:
    keys = [s.key for s in st.SETTINGS]
    assert len(keys) == len(set(keys))
    assert st.validate_effective(st.defaults()) == {}


def test_global_only_scopes() -> None:
    global_keys = {
        "outdoor_temperature_sensor_entity",
        "outdoor_humidity_sensor_entity",
        "outdoor_weather_entity",
        "notify_actions",
        "notification_language",
        "condition_delay",
        "goal_reached_delay",
        "minimum_temperature_difference",
        "minimum_humidity_difference",
        "window_open_debounce",
        "window_close_debounce",
        "problem_report_delay",
    }
    assert {s.key for s in st.SETTINGS if s.scope is Scope.GLOBAL} == global_keys
    assert not global_keys.intersection(st.overridable_keys())
    for key in (
        "comfort_lower_temperature",
        "reference_airing_time",
        "airing_interval",
    ):
        assert key in st.overridable_keys()


def test_numbers_have_ranges_and_defaults_inside() -> None:
    for setting in st.SETTINGS:
        if setting.kind is Kind.NUMBER:
            assert setting.minimum is not None and setting.maximum is not None
            assert setting.minimum <= setting.default <= setting.maximum, setting.key


def test_merge_sources_and_ignores_global_only_overrides() -> None:
    effective, sources = st.merge(
        {"comfort_upper_humidity": 70},
        {
            "comfort_lower_humidity": 50,
            "notify_actions": ["notify.x"],
            "outdoor_temperature_sensor_entity": "sensor.local",
            "condition_delay": 0,
            "unknown": 1,
        },
    )
    assert effective["comfort_upper_humidity"] == 70
    assert sources["comfort_upper_humidity"] == "global"
    assert effective["comfort_lower_humidity"] == 50
    assert sources["comfort_lower_humidity"] == "room"
    assert effective["notify_actions"] == []
    assert effective["outdoor_temperature_sensor_entity"] is None
    assert effective["condition_delay"] == 5
    assert "unknown" not in effective


def test_scenario_defaults() -> None:
    defaults = st.defaults()
    assert len(st.SCENARIOS) == 5 and len(st.PROTECT_SCENARIOS) == 4
    for prefix in st.SCENARIOS:
        assert defaults[f"{prefix}_notification_enabled"]
        assert defaults[f"{prefix}_goal_reached_notification_enabled"]
        assert defaults[f"{prefix}_reminder_interval"] == 120
        assert defaults[f"{prefix}_goal_reached_reminder_interval"] == 0
        assert not defaults[f"{prefix}_notify_during_quiet_hours"]
        assert not defaults[f"{prefix}_goal_reached_notify_during_quiet_hours"]
    for prefix in st.PROTECT_SCENARIOS:
        assert defaults[f"{prefix}_notification_enabled"] is (prefix != "moisture_loss")
        assert defaults[f"{prefix}_reminder_interval"] == 15
        assert f"{prefix}_goal_reached_notification_enabled" not in defaults
        assert defaults[f"{prefix}_notify_during_quiet_hours"] is (
            prefix == "heat_loss"
        )
    expected = {
        "comfort_lower_temperature": 19,
        "comfort_upper_temperature": 23,
        "comfort_lower_humidity": 40,
        "comfort_upper_humidity": 55,
        "reference_airing_time": 10,
        "maximum_airing_time_factor": 1.5,
        "airing_time_lower_limit": 5,
        "airing_time_upper_limit": 45,
        "airing_interval": 8,
        "condition_delay": 5,
        "goal_reached_delay": 1,
        "minimum_temperature_difference": 2,
        "minimum_humidity_difference": 5,
        "window_open_debounce": 60,
        "window_close_debounce": 30,
        "problem_report_delay": 10,
    }
    assert expected.items() <= defaults.items()
    assert st.SETTINGS_BY_KEY["airing_interval"].unit == "h"
    assert st.SETTINGS_BY_KEY["airing_interval"].step == 0.1
    assert st.validate_value(st.SETTINGS_BY_KEY["airing_interval"], 24) is None
    assert st.SETTINGS_BY_KEY["airing_interval"].section == "overdue_airing"
    assert st.defaults()["overdue_airing_reminder_interval"] == 120
    assert st.defaults()["airing_interval"] == 8


@pytest.mark.parametrize("prefix", (*st.SCENARIOS, *st.PROTECT_SCENARIOS))
def test_quiet_permission_supports_room_override(prefix: str) -> None:
    key = f"{prefix}_notify_during_quiet_hours"
    assert st.SETTINGS_BY_KEY[key].scope is st.Scope.SHARED
    effective, sources = st.merge({key: True}, {key: False})
    assert effective[key] is False
    assert sources[key] == "room"
    assert not st.validate_effective(effective)


def test_no_legacy_settings_or_aliases() -> None:
    retired = {
        "drying_possible_start_relative_humidity",
        "drying_possible_target_relative_humidity",
        "drying_possible_min_absolute_humidity_difference",
        "drying_possible_use_thermostat_target_temperature",
        "cooling_possible_start_indoor_temperature",
        "cooling_possible_target_indoor_temperature",
        "cooling_possible_min_temperature_difference",
        "cooling_possible_min_outdoor_temperature",
        "overdue_airing_interval",
        "heat_loss_reminder_interval_ratio",
        "recommended_airing_time_shortest",
        "maximum_airing_time_longest",
    }
    retired |= {
        f"{prefix}_{suffix}"
        for prefix in st.SCENARIOS
        for suffix in (
            "open_notification_enabled",
            "close_notification_enabled",
            "open_notification_delay",
            "close_notification_delay",
            "open_reminder_interval",
            "close_reminder_interval",
        )
    }
    assert not retired.intersection(st.defaults())
    assert not retired.intersection(st.global_settings(dict.fromkeys(retired, 1)))


@pytest.mark.parametrize("prefix", st.SCENARIOS)
def test_goal_requires_main_notification(prefix: str) -> None:
    effective, _ = st.merge({}, {f"{prefix}_notification_enabled": False})
    goal = f"{prefix}_goal_reached_notification_enabled"
    assert (
        st.validate_effective(effective)[goal]
        == f"requires_{prefix}_notification_enabled"
    )
    effective[goal] = False
    assert st.validate_effective(effective) == {}


def test_ordering_constraints() -> None:
    for low, high, allow_equal in st.ORDERING_RULES:
        effective = st.defaults()
        effective[low] = effective[high] + 1
        assert low in st.validate_effective(effective), (low, high)
        effective[low] = effective[high]
        assert (low in st.validate_effective(effective)) is (not allow_equal)


@pytest.mark.parametrize(
    "key,value",
    [
        ("reference_airing_time", 0),
        ("airing_time_lower_limit", 0),
        ("airing_time_upper_limit", -1),
        ("maximum_airing_time_factor", 0.99),
        ("airing_interval", 0),
        ("minimum_temperature_difference", 0),
        ("minimum_humidity_difference", 0),
        ("reference_airing_time", float("nan")),
        ("reference_airing_time", float("inf")),
    ],
)
def test_invalid_model_values(key: str, value: float) -> None:
    assert key in st.validate_effective(st.defaults() | {key: value})


@pytest.mark.parametrize("difference", [None, 0, 0.5, 2, 5, 10, 25, 100, -10])
def test_review_duration_matches_physics(difference: float | None) -> None:
    from custom_components.ventilation_advisor.physics import (
        recommended_airing_duration_s,
    )

    effective = st.defaults()
    expected = recommended_airing_duration_s(difference, 10, 5, 45)
    assert st.airing_duration_s(effective, "recommended", difference) == expected
    assert st.airing_duration_s(effective, "maximum", difference) == expected * 1.5


def test_review_duration_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        st.airing_duration_s(st.defaults(), "unknown", 10)


def test_validate_value_kinds() -> None:
    by = st.SETTINGS_BY_KEY
    assert st.validate_value(by["quiet_hours_enabled"], 1) == "invalid_value"
    assert st.validate_value(by["airing_interval"], True) == "invalid_value"
    assert st.validate_value(by["airing_interval"], 10_000) == "out_of_range"
    assert st.validate_value(by["quiet_hours_start"], "25:00") == "invalid_time"
    assert st.validate_value(by["quiet_hours_start"], "06:30") is None
    assert st.validate_value(by["notify_actions"], ["light.x"]) == "invalid_value"
    assert st.validate_value(by["notify_actions"], ["notify.a"]) is None
    for action in ("notify.send_message", "notify.persistent_notification", "notify."):
        assert st.validate_value(by["notify_actions"], [action]) == "invalid_value"
    assert st.validate_value(by["notification_language"], "xx") == "invalid_value"
    assert st.validate_value(by["notification_language"], "de") is None
    assert st.validate_value(by["outdoor_temperature_sensor_entity"], None) is None
    for value in ("light.x", "sensor", "sensor."):
        assert (
            st.validate_value(by["outdoor_temperature_sensor_entity"], value)
            == "invalid_entity"
        )
    assert (
        st.validate_value(by["outdoor_temperature_sensor_entity"], "sensor.t") is None
    )
    assert st.validate_value(by["outdoor_weather_entity"], "weather.home") is None
    assert (
        st.validate_value(by["outdoor_temperature_sensor_entity"], "weather.home")
        == "invalid_entity"
    )
    assert (
        st.validate_value(by["outdoor_weather_entity"], "sensor.t") == "invalid_entity"
    )


def test_parse_time() -> None:
    assert st.parse_time("22:00:00") == (22, 0)
    assert st.parse_time("7:05") == (7, 5)
    for value in ("24:00", "ab:cd", "22:00:60", None):
        assert st.parse_time(value) is None
