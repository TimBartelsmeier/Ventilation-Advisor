"""Single definition of user settings, merging and validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any

from .const import (
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMPERATURE,
    CONF_OUTDOOR_WEATHER,
)

SETTINGS_VERSION = 1


class Kind(StrEnum):
    BOOL = "bool"
    NUMBER = "number"
    TIME = "time"
    ENTITY = "entity"
    NOTIFY_LIST = "notify_list"
    LANGUAGE = "language"


class Scope(StrEnum):
    GLOBAL = "global"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    kind: Kind
    default: Any
    section: str
    label: str
    description: str = ""
    scope: Scope = Scope.SHARED
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    group: str | None = None
    entity_domains: tuple[str, ...] = field(default_factory=tuple)


SCENARIOS = (
    "warming_possible",
    "cooling_possible",
    "drying_possible",
    "humidifying_possible",
    "overdue_airing",
)
PROTECT_SCENARIOS = ("heat_loss", "heat_gain", "moisture_loss", "moisture_gain")
SECTIONS = (
    "outdoor",
    "comfort",
    "airing_time",
    *SCENARIOS,
    *PROTECT_SCENARIOS,
    "notifications",
    "quiet_hours",
    "reminders",
    "airing_time_advanced",
    "advanced",
)
SECTION_LABELS = {
    "outdoor": "Outdoor sources",
    "comfort": "Comfort window",
    "airing_time": "Airing time",
    "warming_possible": "Warming possible",
    "cooling_possible": "Cooling possible",
    "drying_possible": "Drying possible",
    "humidifying_possible": "Humidifying possible",
    "overdue_airing": "Overdue airing",
    "heat_loss": "Heat loss",
    "heat_gain": "Heat gain",
    "moisture_loss": "Moisture loss",
    "moisture_gain": "Moisture gain",
    "notifications": "Notifications",
    "quiet_hours": "Quiet hours",
    "reminders": "Reminders",
    "airing_time_advanced": "Airing time – advanced",
    "advanced": "Advanced",
}
SCENARIO_DESCRIPTIONS = {
    "warming_possible": (
        "Suggest opening when outdoor air can warm the room toward its comfort "
        "range or heating setpoint."
    ),
    "cooling_possible": (
        "Suggest opening when outdoor air can cool the room toward its comfort range."
    ),
    "drying_possible": (
        "Suggest opening when outdoor air can reduce excess humidity, compared at "
        "the indoor temperature or heating setpoint."
    ),
    "humidifying_possible": (
        "Suggest opening when outdoor air can raise low humidity, compared at "
        "the indoor temperature or heating setpoint."
    ),
    "overdue_airing": (
        "Suggest opening when no qualifying airing has occurred within the airing "
        "interval. This sets how often airing is due; the reminder interval controls "
        "repeat notifications while it remains overdue."
    ),
    "heat_loss": (
        "After the maximum airing time, suggest closing if further airing risks "
        "cooling below the effective lower temperature bound."
    ),
    "heat_gain": (
        "After the maximum airing time, suggest closing if further airing risks "
        "warming above the effective upper temperature bound."
    ),
    "moisture_loss": (
        "After the maximum airing time, suggest closing if further airing risks "
        "making the room too dry."
    ),
    "moisture_gain": (
        "After the maximum airing time, suggest closing if further airing risks "
        "making the room too humid."
    ),
}
QUIET_GROUP = "quiet_hours_times"


def _number(
    key: str,
    default: float,
    section: str,
    label: str,
    minimum: float,
    maximum: float | None,
    step: float,
    unit: str | None = None,
    description: str = "",
    *,
    scope: Scope = Scope.SHARED,
) -> Setting:
    return Setting(
        key,
        Kind.NUMBER,
        default,
        section,
        label,
        description,
        scope=scope,
        minimum=minimum,
        maximum=maximum,
        step=step,
        unit=unit,
    )


def _scenario_settings(prefix: str) -> tuple[Setting, ...]:
    thermal = prefix in ("heat_loss", "heat_gain")
    fields = [
        Setting(
            f"{prefix}_notification_enabled",
            Kind.BOOL,
            prefix != "moisture_loss",
            prefix,
            "Send notifications",
            "Enable this scenario's open suggestions."
            if prefix in SCENARIOS
            else "Suggest closing only after the maximum airing time, when outdoor air is adverse at a comfort-window edge.",
        ),
        Setting(
            f"{prefix}_notify_during_quiet_hours",
            Kind.BOOL,
            prefix == "heat_loss",
            prefix,
            "Notify during quiet hours",
            "Allow this scenario's suggestions and reminders during quiet hours.",
        ),
        _number(
            f"{prefix}_reminder_factor" if thermal else f"{prefix}_reminder_interval",
            0.75 if thermal else (120.0 if prefix in SCENARIOS else 15.0),
            prefix,
            "Reminder interval factor" if thermal else "Reminder interval",
            0,
            10 if thermal else 1440,
            0.05 if thermal else 5,
            None if thermal else "min",
            (
                "Repeat after this multiple of the opening's maximum airing time, "
                "subject to the advanced minimum thermal reminder interval. "
                "0 disables reminders."
                if thermal
                else "Delay between repeat notifications while airing is overdue; "
                "independent of how often airing is due. 0 disables reminders."
                if prefix == "overdue_airing"
                else "0 disables reminders for this reason."
            ),
        ),
    ]
    if prefix in SCENARIOS:
        fields.extend(
            (
                Setting(
                    f"{prefix}_goal_reached_notification_enabled",
                    Kind.BOOL,
                    True,
                    prefix,
                    "Goal-reached notification",
                    "Requires this scenario's notifications. When all enabled opening goals are reached, you may close the window; airing longer is fine. Goals are independent of later adverse effects.",
                ),
                Setting(
                    f"{prefix}_goal_reached_notify_during_quiet_hours",
                    Kind.BOOL,
                    False,
                    prefix,
                    "Notify goal reached during quiet hours",
                    "Allow this scenario's goal-reached notifications and reminders during quiet hours.",
                ),
                _number(
                    f"{prefix}_goal_reached_reminder_interval",
                    0.0,
                    prefix,
                    "Goal-reached reminder interval",
                    0,
                    1440,
                    5,
                    "min",
                    "0 disables reminders for this reached goal.",
                ),
            )
        )
    return tuple(fields)


SETTINGS: tuple[Setting, ...] = (
    Setting(
        CONF_OUTDOOR_TEMPERATURE,
        Kind.ENTITY,
        None,
        "outdoor",
        "Outdoor temperature sensor",
        "Optional. Used instead of the weather entity for outdoor temperature.",
        scope=Scope.GLOBAL,
        entity_domains=("sensor",),
    ),
    Setting(
        CONF_OUTDOOR_HUMIDITY,
        Kind.ENTITY,
        None,
        "outdoor",
        "Outdoor humidity sensor",
        "Optional. Used instead of the weather entity for outdoor humidity.",
        scope=Scope.GLOBAL,
        entity_domains=("sensor",),
    ),
    Setting(
        CONF_OUTDOOR_WEATHER,
        Kind.ENTITY,
        None,
        "outdoor",
        "Weather entity",
        "Optional fallback for outdoor temperature and humidity when their sensors are not configured. A weather entity or outdoor temperature sensor is required.",
        scope=Scope.GLOBAL,
        entity_domains=("weather",),
    ),
    _number(
        "comfort_lower_temperature",
        19,
        "comfort",
        "Lower temperature",
        -20,
        50,
        0.5,
        "°C",
        "Must be below the upper temperature. A heating setpoint lifts the effective lower bound to max(lower comfort limit, setpoint).",
    ),
    _number(
        "comfort_upper_temperature",
        23,
        "comfort",
        "Upper temperature",
        -20,
        50,
        0.5,
        "°C",
        "The effective upper bound is max(upper comfort limit, effective lower bound + 1 °C).",
    ),
    _number(
        "comfort_lower_humidity",
        40,
        "comfort",
        "Lower relative humidity",
        0,
        100,
        1,
        "%",
        "Must be below the upper humidity. Indoor and outdoor humidity are judged at max(indoor temperature, heating setpoint).",
    ),
    _number(
        "comfort_upper_humidity",
        55,
        "comfort",
        "Upper relative humidity",
        0,
        100,
        1,
        "%",
        "The humidity window is unchanged by heating. Fixed hysteresis is 0.5 °C / 3 %RH, capped at one quarter of each window's width.",
    ),
    _number(
        "reference_airing_time",
        10,
        "airing_time",
        "Reference airing time",
        0.1,
        120,
        0.5,
        "min",
        "Recommended time at a 10 °C indoor/outdoor difference. Time scales with the square root of 10 divided by the temperature difference, clamped to the advanced limits. Increase for large rooms or small/tilted windows.",
    ),
    _number(
        "maximum_airing_time_factor",
        1.5,
        "airing_time",
        "Maximum airing time factor",
        1,
        10,
        0.1,
        description="Maximum time = recommended time × this factor. Both times are fixed when opening is confirmed. Adverse-effect close suggestions never start before the maximum time.",
    ),
    _number(
        "airing_interval",
        48,
        "overdue_airing",
        "Airing interval",
        1,
        None,
        0.1,
        "h",
        "Time between required airings (minimum 1 hour, no maximum). The 48-hour default deliberately avoids frequent alerts for infrequently used rooms; decrease it for frequently used rooms. Repeat notifications use the separate Reminder interval.",
    ),
    *tuple(
        s
        for prefix in (*SCENARIOS, *PROTECT_SCENARIOS)
        for s in _scenario_settings(prefix)
    ),
    Setting(
        "notifications_enabled",
        Kind.BOOL,
        True,
        "notifications",
        "Send notifications",
        "Turn off to suppress advice notifications.",
    ),
    Setting(
        "persistent_notifications_enabled",
        Kind.BOOL,
        True,
        "notifications",
        "Home Assistant notifications",
        "Show advice and problems as persistent notifications.",
    ),
    Setting(
        "notify_actions",
        Kind.NOTIFY_LIST,
        (),
        "notifications",
        "Legacy notify actions",
        "Modern notify entities (notify.send_message) are not supported. Replacing "
        "and clearing works only with Companion App devices or groups of them. "
        "Use the separate Home Assistant notifications option for persistent advice.",
        scope=Scope.GLOBAL,
    ),
    Setting(
        "notify_update_on_reason_change",
        Kind.BOOL,
        True,
        "notifications",
        "Update notify messages when reasons change",
        "Phones may alert again on replacement.",
    ),
    Setting(
        "notification_language",
        Kind.LANGUAGE,
        "en",
        "notifications",
        "Notification language",
        scope=Scope.GLOBAL,
    ),
    Setting(
        "quiet_hours_enabled",
        Kind.BOOL,
        True,
        "quiet_hours",
        "Quiet hours",
        "Defer all advice notifications, including adverse-effect close suggestions. Current advice is evaluated when quiet hours end.",
    ),
    Setting(
        "quiet_hours_start",
        Kind.TIME,
        "22:00:00",
        "quiet_hours",
        "Quiet hours start",
        group=QUIET_GROUP,
    ),
    Setting(
        "quiet_hours_end",
        Kind.TIME,
        "07:00:00",
        "quiet_hours",
        "Quiet hours end",
        "Equal start and end means no quiet hours.",
        group=QUIET_GROUP,
    ),
    Setting(
        "reminders_enabled",
        Kind.BOOL,
        True,
        "reminders",
        "Reminders",
        "Repeat a notification while the same advice remains relevant.",
    ),
    _number(
        "airing_time_lower_limit",
        5,
        "airing_time_advanced",
        "Shortest recommended airing time",
        0.1,
        120,
        0.5,
        "min",
        "Positive lower clamp; must not exceed the upper clamp.",
    ),
    _number(
        "airing_time_upper_limit",
        45,
        "airing_time_advanced",
        "Longest recommended airing time",
        0.1,
        240,
        0.5,
        "min",
        "Positive upper clamp; also used when either temperature is missing.",
    ),
    _number(
        "minimum_thermal_reminder_interval",
        3,
        "advanced",
        "Minimum thermal reminder interval",
        0.1,
        1440,
        0.1,
        "min",
        "Minimum time between Heat loss or Heat gain reminders. The interval is the larger of this value and the scenario's factor × the opening's maximum airing time. A zero factor still disables reminders.",
    ),
    _number(
        "condition_delay",
        5,
        "advanced",
        "Condition delay",
        0,
        60,
        1,
        "min",
        "How long conditions must hold before an open suggestion.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "goal_reached_delay",
        1,
        "advanced",
        "Goal-reached delay",
        0,
        60,
        1,
        "min",
        "How long a comfort goal's reached condition must hold.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "minimum_temperature_difference",
        2,
        "advanced",
        "Minimum temperature difference",
        0.1,
        40,
        0.5,
        "K",
        "Meaningful outdoor-air pull; released below half this difference.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "minimum_humidity_difference",
        5,
        "advanced",
        "Minimum humidity difference",
        0.1,
        100,
        1,
        "%RH",
        "Meaningful outdoor-air pull at the heating-adjusted reference temperature; released below half this difference.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "window_open_debounce",
        60,
        "advanced",
        "Window open delay",
        0,
        600,
        5,
        "s",
        "How long the room must stay open before the opening is confirmed.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "window_close_debounce",
        30,
        "advanced",
        "Window close delay",
        0,
        600,
        5,
        "s",
        "How long the room must stay closed before the opening ends.",
        scope=Scope.GLOBAL,
    ),
    _number(
        "problem_report_delay",
        10,
        "advanced",
        "Problem report delay",
        0,
        60,
        1,
        "min",
        "How long an input must be invalid before a Repairs issue is raised. Dependent scenarios freeze until recovery. 0 reports immediately.",
        scope=Scope.GLOBAL,
    ),
)

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}
LANGUAGES = ("en", "de")
ORDERING_RULES = (
    ("comfort_lower_temperature", "comfort_upper_temperature", False),
    ("comfort_lower_humidity", "comfort_upper_humidity", False),
    ("airing_time_lower_limit", "airing_time_upper_limit", True),
)


def defaults() -> dict[str, Any]:
    return {
        s.key: list(s.default) if isinstance(s.default, tuple) else s.default
        for s in SETTINGS
    }


def global_settings(options: Mapping[str, Any]) -> dict[str, Any]:
    result = defaults()
    result.update({k: v for k, v in options.items() if k in SETTINGS_BY_KEY})
    return result


def overridable_keys() -> list[str]:
    return [s.key for s in SETTINGS if s.scope is Scope.SHARED]


def merge(
    options: Mapping[str, Any], overrides: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    effective = global_settings(options)
    sources = {key: "global" for key in effective}
    for key, value in overrides.items():
        setting = SETTINGS_BY_KEY.get(key)
        if setting is not None and setting.scope is Scope.SHARED:
            effective[key] = value
            sources[key] = "room"
    return effective, sources


def validate_value(setting: Setting, value: Any) -> str | None:
    if setting.kind is Kind.BOOL:
        return None if isinstance(value, bool) else "invalid_value"
    if setting.kind is Kind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "invalid_value"
        assert setting.minimum is not None
        return (
            None
            if isfinite(value)
            and value >= setting.minimum
            and (setting.maximum is None or value <= setting.maximum)
            else "out_of_range"
        )
    if setting.kind is Kind.TIME:
        return None if parse_time(value) is not None else "invalid_time"
    if setting.kind is Kind.LANGUAGE:
        return None if value in LANGUAGES else "invalid_value"
    if setting.kind is Kind.NOTIFY_LIST:
        return (
            None
            if isinstance(value, (list, tuple))
            and all(
                isinstance(v, str)
                and v.startswith("notify.")
                and len(v.split(".", 1)[1]) > 0
                and v not in ("notify.send_message", "notify.persistent_notification")
                for v in value
            )
            else "invalid_value"
        )
    if setting.kind is Kind.ENTITY:
        return (
            None
            if value is None
            or (
                isinstance(value, str)
                and "." in value
                and value.split(".", 1)[0] in setting.entity_domains
                and bool(value.split(".", 1)[1])
            )
            else "invalid_entity"
        )
    return "invalid_value"


def validate_effective(effective: Mapping[str, Any]) -> dict[str, str]:
    errors = {
        s.key: err
        for s in SETTINGS
        if (err := validate_value(s, effective.get(s.key))) is not None
    }
    for low, high, allow_equal in ORDERING_RULES:
        if (
            low not in errors
            and high not in errors
            and (
                effective[low] > effective[high]
                or (effective[low] == effective[high] and not allow_equal)
            )
        ):
            relation = "must_be_at_most" if allow_equal else "must_be_below"
            errors[low] = f"{relation}_{high}"
    for prefix in SCENARIOS:
        enabled = f"{prefix}_notification_enabled"
        goal = f"{prefix}_goal_reached_notification_enabled"
        if (
            enabled not in errors
            and goal not in errors
            and effective[goal]
            and not effective[enabled]
        ):
            errors[goal] = f"requires_{enabled}"
    return errors


def airing_duration_s(
    effective: Mapping[str, Any], kind: str, difference: float | None
) -> float:
    """Evaluate the opening-time model for the configuration review."""
    from .physics import recommended_airing_duration_s

    duration = recommended_airing_duration_s(
        difference,
        effective["reference_airing_time"],
        effective["airing_time_lower_limit"],
        effective["airing_time_upper_limit"],
    )
    if kind == "maximum":
        duration *= effective["maximum_airing_time_factor"]
    elif kind != "recommended":
        raise ValueError(kind)
    return duration


def parse_time(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    if len(parts) == 3 and not 0 <= int(parts[2]) < 60:
        return None
    return hour, minute
