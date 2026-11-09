"""Constants for the Ventilation Advisor integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "ventilation_advisor"
SUBENTRY_ROOM: Final = "room"

STORAGE_KEY: Final = f"{DOMAIN}.rooms"
STORAGE_VERSION: Final = 2

# Room subentry data keys (per-room only).
CONF_ROOM_ID: Final = "room_id"
CONF_NAME: Final = "name"
CONF_AREA_ID: Final = "area_id"
CONF_NAME_OVERRIDE: Final = "name_override"
CONF_FILTER_BY_AREA: Final = "filter_by_area"
CONF_ADVISOR_ENABLED: Final = "advisor_enabled"
CONF_CONTACTS: Final = "contacts"
CONF_INDOOR_TEMPERATURE: Final = "indoor_temperature_entity"
CONF_INDOOR_HUMIDITY: Final = "indoor_humidity_entity"
CONF_THERMOSTAT: Final = "thermostat_entity"
CONF_OVERRIDES: Final = "overrides"

# Shared (G/R) entity bindings.
CONF_OUTDOOR_TEMPERATURE: Final = "outdoor_temperature_sensor_entity"
CONF_OUTDOOR_HUMIDITY: Final = "outdoor_humidity_sensor_entity"
CONF_OUTDOOR_WEATHER: Final = "outdoor_weather_entity"

PERSISTENT_TARGET: Final = "persistent_notification"
CLEAR_NOTIFICATION: Final = "clear_notification"

# Fixed safeguards, intentionally not user options.
HYSTERESIS_K: Final = 0.5
HYSTERESIS_RH: Final = 3.0
TEMPERATURE_MIN_C: Final = -80.0
TEMPERATURE_MAX_C: Final = 80.0
RETRY_DELAYS_S: Final = (30.0, 120.0, 300.0)
CONTENT_REFRESH_MIN_S: Final = 60.0
RECONCILE_INTERVAL_S: Final = 15 * 60


class Recommendation(StrEnum):
    """Recommendation states. HA ``unknown`` is represented by ``None``."""

    NONE = "none"
    OPEN = "open"
    CLOSE = "close"
    PAUSED = "paused"


ACTIONABLE = frozenset(
    {
        Recommendation.OPEN,
        Recommendation.CLOSE,
    }
)


class Reason(StrEnum):
    """Reason identifiers."""

    DRYING_POSSIBLE = "drying_possible"
    OVERDUE_AIRING = "overdue_airing"
    COOLING_POSSIBLE = "cooling_possible"
    WARMING_POSSIBLE = "warming_possible"
    HUMIDIFYING_POSSIBLE = "humidifying_possible"
    HEAT_LOSS = "heat_loss"
    HEAT_GAIN = "heat_gain"
    MOISTURE_LOSS = "moisture_loss"
    MOISTURE_GAIN = "moisture_gain"


class ScenarioStatus(StrEnum):
    """Typed scenario status."""

    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"
    INACTIVE = "inactive"
    SETTLING = "settling"
    ACTIVE = "active"
    RELEASING = "releasing"
    RESOLVED = "resolved"


class Resolution(StrEnum):
    """Resolution codes for participating reasons."""

    DRYING_POSSIBLE_TARGET_REACHED = "drying_possible_target_reached"
    DRYING_POSSIBLE_NO_LONGER_HELPFUL = "drying_possible_no_longer_helpful"
    COOLING_POSSIBLE_TARGET_REACHED = "cooling_possible_target_reached"
    COOLING_POSSIBLE_NO_LONGER_HELPFUL = "cooling_possible_no_longer_helpful"
    WARMING_POSSIBLE_TARGET_REACHED = "warming_possible_target_reached"
    WARMING_POSSIBLE_NO_LONGER_HELPFUL = "warming_possible_no_longer_helpful"
    HUMIDIFYING_POSSIBLE_TARGET_REACHED = "humidifying_possible_target_reached"
    HUMIDIFYING_POSSIBLE_NO_LONGER_HELPFUL = "humidifying_possible_no_longer_helpful"
    AIRED = "aired"


class WindowState(StrEnum):
    """Aggregated window state."""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class ReferenceOrigin(StrEnum):
    """Origin of an indoor reference temperature."""

    INDOOR = "indoor"
    THERMOSTAT_TARGET = "thermostat_target"
    ASSUMED = "assumed"
