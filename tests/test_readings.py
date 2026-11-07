"""Validate Home Assistant input normalization at the engine boundary."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ventilation_advisor.readings import (
    read_contact,
    read_humidity,
    read_temperature,
    read_thermostat,
)

from .conftest import set_state


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [("68", "°F", 20), ("293.15", "K", 20), ("20", "°C", 20)],
)
async def test_temperature_conversion(
    hass: HomeAssistant, raw: str, unit: str, expected: float
) -> None:
    set_state(hass, "sensor.temperature", raw, {"unit_of_measurement": unit})
    reading = read_temperature(hass, "sensor.temperature")
    assert reading.valid
    assert reading.value == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "unit", "error"),
    [
        ("20", None, "unsupported_unit"),
        ("20", "C", "unsupported_unit"),
        ("bad", "°C", "not_numeric"),
        ("nan", "°C", "not_numeric"),
        ("inf", "°C", "not_numeric"),
        ("-inf", "°C", "not_numeric"),
        ("-81", "°C", "out_of_range"),
        ("81", "°C", "out_of_range"),
    ],
)
async def test_invalid_temperature(
    hass: HomeAssistant, raw: str, unit: str | None, error: str
) -> None:
    set_state(hass, "sensor.temperature", raw, {"unit_of_measurement": unit})
    reading = read_temperature(hass, "sensor.temperature")
    assert not reading.valid
    assert reading.error == error


@pytest.mark.parametrize(
    ("raw", "unit", "error"),
    [
        ("50", None, "unsupported_unit"),
        ("50", "RH", "unsupported_unit"),
        ("-1", "%", "out_of_range"),
        ("101", "%", "out_of_range"),
        ("bad", "%", "not_numeric"),
        ("nan", "%", "not_numeric"),
        ("inf", "%", "not_numeric"),
        ("-inf", "%", "not_numeric"),
    ],
)
async def test_invalid_humidity(
    hass: HomeAssistant, raw: str, unit: str | None, error: str
) -> None:
    set_state(hass, "sensor.humidity", raw, {"unit_of_measurement": unit})
    reading = read_humidity(hass, "sensor.humidity")
    assert not reading.valid
    assert reading.error == error


@pytest.mark.parametrize("value", [0, 50, 100])
async def test_valid_humidity(hass: HomeAssistant, value: int) -> None:
    set_state(hass, "sensor.humidity", str(value), {"unit_of_measurement": "%"})
    assert read_humidity(hass, "sensor.humidity").value == value


async def test_weather_uses_attributes(hass: HomeAssistant) -> None:
    set_state(
        hass,
        "weather.home",
        "rainy",
        {"temperature": 68, "temperature_unit": "°F", "humidity": 72},
    )
    assert read_temperature(hass, "weather.home").value == pytest.approx(20)
    assert read_humidity(hass, "weather.home").value == 72


@pytest.mark.parametrize(
    ("attributes", "temperature_error", "humidity_error"),
    [
        ({}, "not_numeric", "not_numeric"),
        ({"temperature": 20, "humidity": 50}, "unsupported_unit", None),
        (
            {"temperature": True, "temperature_unit": "°C", "humidity": False},
            "not_numeric",
            "not_numeric",
        ),
        (
            {"temperature": float("inf"), "temperature_unit": "°C", "humidity": 101},
            "not_numeric",
            "out_of_range",
        ),
    ],
)
async def test_invalid_weather_attributes(
    hass: HomeAssistant,
    attributes: dict,
    temperature_error: str | None,
    humidity_error: str | None,
) -> None:
    set_state(hass, "weather.home", "sunny", attributes)
    assert read_temperature(hass, "weather.home").error == temperature_error
    assert read_humidity(hass, "weather.home").error == humidity_error


@pytest.mark.parametrize("reader", [read_temperature, read_humidity, read_thermostat])
async def test_unconfigured_and_missing_sources(hass: HomeAssistant, reader) -> None:
    assert reader(hass, None) is None
    assert reader(hass, "") is None
    assert reader(hass, "sensor.missing").error == "missing"


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
async def test_unavailable_sources(hass: HomeAssistant, state: str) -> None:
    set_state(hass, "sensor.input", state)
    for reader in (read_temperature, read_humidity, read_thermostat):
        assert reader(hass, "sensor.input").error == state


@pytest.mark.parametrize(
    ("mode", "heating", "target"),
    [
        ("heat", True, 18),
        ("heat_cool", True, 18),
        ("auto", True, 18),
        ("cool", False, None),
        ("off", False, None),
    ],
)
async def test_climate_mode_target_selection(
    hass: HomeAssistant, mode: str, heating: bool, target: float | None
) -> None:
    set_state(
        hass,
        "climate.room",
        mode,
        {
            "temperature": 22,
            "target_temp_low": 18,
            "target_temp_high": 26,
            "temperature_unit": "°C",
            "hvac_action": "idle",
        },
    )
    reading = read_thermostat(hass, "climate.room")
    assert reading.error is None
    assert reading.heating is heating
    assert reading.target == target


@pytest.mark.parametrize(("unit", "target"), [("°F", 68), ("K", 293.15)])
async def test_climate_target_conversion(
    hass: HomeAssistant, unit: str, target: float
) -> None:
    set_state(
        hass,
        "climate.room",
        "heat",
        {"temperature": target, "temperature_unit": unit},
    )
    assert read_thermostat(hass, "climate.room").target == pytest.approx(20)


async def test_climate_unit_falls_back_to_system(hass: HomeAssistant) -> None:
    set_state(hass, "climate.room", "heat", {"temperature": 21})
    assert read_thermostat(hass, "climate.room").target == 21


async def test_inactive_climate_ignores_invalid_target(hass: HomeAssistant) -> None:
    set_state(hass, "climate.room", "off", {"temperature": "bad"})
    reading = read_thermostat(hass, "climate.room")
    assert not reading.heating
    assert reading.target is None
    assert reading.error is None


@pytest.mark.parametrize("mode", ["heat", "heat_cool", "auto"])
async def test_climate_missing_target_preserves_heating_mode(
    hass: HomeAssistant, mode: str
) -> None:
    set_state(hass, "climate.room", mode, {"target_temp_high": 25})
    reading = read_thermostat(hass, "climate.room")
    assert reading.error is None
    assert reading.heating
    assert reading.target is None


@pytest.mark.parametrize(
    ("target", "unit", "error"),
    [
        ("bad", "°C", "not_numeric"),
        (True, "°C", "not_numeric"),
        (float("nan"), "°C", "not_numeric"),
        (float("inf"), "°C", "not_numeric"),
        (21, "C", "unsupported_unit"),
        (100, "°C", "out_of_range"),
    ],
)
async def test_invalid_thermostat_clears_heating_reference(
    hass: HomeAssistant, target: object, unit: str, error: str
) -> None:
    set_state(
        hass,
        "climate.room",
        "heat",
        {"temperature": target, "temperature_unit": unit},
    )
    reading = read_thermostat(hass, "climate.room")
    assert reading.error == error
    assert reading.target is None
    assert not reading.heating


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("on", True),
        ("off", False),
        ("unknown", None),
        ("unavailable", None),
        ("open", None),
    ],
)
async def test_contact_states(
    hass: HomeAssistant, state: str, expected: bool | None
) -> None:
    set_state(hass, "binary_sensor.window", state)
    assert read_contact(hass, "binary_sensor.window") is expected
    assert read_contact(hass, "binary_sensor.missing") is None
