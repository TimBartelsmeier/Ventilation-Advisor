"""Convert Home Assistant states into validated engine readings."""

from __future__ import annotations

import math

from homeassistant.const import (
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    STATE_OFF,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import TEMPERATURE_MAX_C, TEMPERATURE_MIN_C
from .engine import Reading, ThermostatReading

_TEMPERATURE_UNITS = {
    UnitOfTemperature.CELSIUS,
    UnitOfTemperature.FAHRENHEIT,
    UnitOfTemperature.KELVIN,
}


def _number(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return value if math.isfinite(value) else None


def _temperature(entity_id: str, raw: object, unit: object) -> Reading:
    value = _number(raw)
    if value is None:
        return Reading(entity_id, error="not_numeric")
    if unit not in _TEMPERATURE_UNITS:
        return Reading(entity_id, error="unsupported_unit")
    celsius = TemperatureConverter.convert(value, str(unit), UnitOfTemperature.CELSIUS)
    if not TEMPERATURE_MIN_C <= celsius <= TEMPERATURE_MAX_C:
        return Reading(entity_id, error="out_of_range")
    return Reading(entity_id, celsius)


def _humidity(entity_id: str, raw: object, unit: object) -> Reading:
    value = _number(raw)
    if value is None:
        return Reading(entity_id, error="not_numeric")
    if unit != PERCENTAGE:
        return Reading(entity_id, error="unsupported_unit")
    if not 0 <= value <= 100:
        return Reading(entity_id, error="out_of_range")
    return Reading(entity_id, value)


def _state(hass: HomeAssistant, entity_id: str) -> State | Reading:
    state = hass.states.get(entity_id)
    if state is None:
        return Reading(entity_id, error="missing")
    if state.state in ("unavailable", "unknown"):
        return Reading(entity_id, error=state.state)
    return state


def read_temperature(hass: HomeAssistant, entity_id: str | None) -> Reading | None:
    """Read a temperature from a sensor or weather entity (in °C)."""
    if not entity_id:
        return None
    state = _state(hass, entity_id)
    if isinstance(state, Reading):
        return state
    if entity_id.startswith("weather."):
        return _temperature(
            entity_id,
            state.attributes.get(ATTR_TEMPERATURE),
            state.attributes.get("temperature_unit"),
        )
    return _temperature(
        entity_id, state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    )


def read_humidity(hass: HomeAssistant, entity_id: str | None) -> Reading | None:
    """Read relative humidity from a sensor or weather entity (in %)."""
    if not entity_id:
        return None
    state = _state(hass, entity_id)
    if isinstance(state, Reading):
        return state
    if entity_id.startswith("weather."):
        return _humidity(entity_id, state.attributes.get("humidity"), PERCENTAGE)
    return _humidity(
        entity_id, state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    )


def read_thermostat(
    hass: HomeAssistant, entity_id: str | None
) -> ThermostatReading | None:
    """Read the HVAC mode and target temperature from a climate entity."""
    if not entity_id:
        return None
    state = _state(hass, entity_id)
    if isinstance(state, Reading):
        return ThermostatReading(entity_id, error=state.error)
    attrs = state.attributes
    heating = state.state in ("heat", "heat_cool", "auto")
    if not heating:
        return ThermostatReading(entity_id)
    raw_target = (
        attrs.get("target_temp_low")
        if attrs.get("target_temp_low") is not None
        else attrs.get(ATTR_TEMPERATURE)
    )
    unit = attrs.get("temperature_unit") or hass.config.units.temperature_unit
    if raw_target is None:
        return ThermostatReading(entity_id, heating=heating)
    target = _temperature(entity_id, raw_target, unit)
    if not target.valid:
        return ThermostatReading(entity_id, error=target.error)
    return ThermostatReading(entity_id, heating=heating, target=target.value)


def read_contact(hass: HomeAssistant, entity_id: str) -> bool | None:
    """Return True for open, False for closed, None if unknown."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state == STATE_ON:
        return True
    if state.state == STATE_OFF:
        return False
    return None
