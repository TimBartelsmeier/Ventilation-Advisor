"""Config, options, and room subentry flow tests."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.ventilation_advisor import settings
from custom_components.ventilation_advisor.config_flow import (
    _notify_options,
    _overrides_schema,
    _room_schema,
    flatten_global,
    global_schema,
    parse_overrides,
    review_summary,
    validate_global,
)
from custom_components.ventilation_advisor.const import DOMAIN

from .conftest import global_options, room_data, set_state, setup_entry


def sectioned(options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert flat options into the sectioned global form input."""
    result: dict[str, dict[str, Any]] = {s: {} for s in settings.SECTIONS}
    for setting in settings.SETTINGS:
        value = options[setting.key]
        if value is None:
            continue
        if setting.kind is settings.Kind.TIME:
            value = value[:5]
        result[setting.section][setting.key] = value
    return result


ROOM_INPUT = {
    "name": "Bedroom",
    "advisor_enabled": True,
    "contacts": ["binary_sensor.bedroom_window"],
    "indoor_temperature_entity": "sensor.bedroom_temperature",
    "indoor_humidity_entity": "sensor.bedroom_humidity",
}


def all_shared() -> dict[str, dict[str, Any]]:
    """Overrides form input keeping every option shared."""
    return {}


def _default(marker: Any) -> Any:
    default = getattr(marker, "default", None)
    if callable(default):
        try:
            return default()
        except TypeError:
            return None
    return default


def fill(result: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    """Complete ``user_input`` with form defaults like the frontend does."""
    data: dict[str, Any] = {}
    for marker, value in result["data_schema"].schema.items():
        key = str(marker)
        nested = getattr(value, "schema", None)
        if nested is not None and hasattr(nested, "schema"):
            section = {}
            for inner in nested.schema:
                inner_default = _default(inner)
                if inner_default is not None:
                    section[str(inner)] = inner_default
            section.update(user_input.get(key, {}))
            data[key] = section
            continue
        if key in user_input:
            data[key] = user_input[key]
        elif (default := _default(marker)) is not None:
            data[key] = default
    return data


async def configure_subentry(hass: HomeAssistant, result, user_input):
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], fill(result, user_input)
    )


def states(hass: HomeAssistant) -> None:
    for entity_id, value, unit in (
        ("binary_sensor.bedroom_window", "off", None),
        ("binary_sensor.office_window", "off", None),
        ("sensor.bedroom_temperature", "21", "°C"),
        ("sensor.bedroom_humidity", "50", "%"),
        ("sensor.outdoor_temperature", "10", "°C"),
        ("sensor.outdoor_humidity", "70", "%"),
        ("sensor.balcony_temperature", "12", "°C"),
    ):
        set_state(
            hass, entity_id, value, {"unit_of_measurement": unit} if unit else None
        )


async def _start(hass: HomeAssistant, options: dict[str, Any] | None = None):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], sectioned(options or global_options())
    )


async def test_setup_without_room(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert (
        entry.options["outdoor_temperature_sensor_entity"]
        == "sensor.outdoor_temperature"
    )
    assert entry.options["quiet_hours_start"] == "22:00:00"
    assert set(entry.options) == {s.key for s in settings.SETTINGS}
    assert not entry.subentries


async def test_setup_accepts_weather_entity_without_outdoor_sensors(
    hass: HomeAssistant,
) -> None:
    options = global_options(
        outdoor_temperature_sensor_entity=None,
        outdoor_humidity_sensor_entity=None,
        outdoor_weather_entity="weather.home",
    )
    result = await _start(hass, options)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["outdoor_weather_entity"] == "weather.home"


async def test_setup_with_first_room(hass: HomeAssistant) -> None:
    states(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_room"}
    )
    assert result["step_id"] == "room"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ROOM_INPUT
    )
    assert result["step_id"] == "overrides"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], fill(result, all_shared())
    )
    assert result["step_id"] == "review"
    summary = result["description_placeholders"]["summary"]
    assert "Bedroom" in summary
    assert "| -4 °C | 25 K | 6.3 min | 9.5 min |" in summary
    assert "| 11 °C | 10 K | 10.0 min | 15.0 min |" in summary
    assert "| 21 °C | 0 K | 45.0 min | 67.5 min |" in summary
    assert "Lower temperature: 19 °C (global)" in summary
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Bedroom"
    assert subentry.data["contacts"] == ["binary_sensor.bedroom_window"]
    assert subentry.data["overrides"] == {}
    await hass.async_block_till_done()


async def test_setup_rejects_inconsistent_thresholds(hass: HomeAssistant) -> None:
    result = await _start(
        hass,
        global_options(
            comfort_lower_humidity=60.0,
            comfort_upper_humidity=50.0,
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_settings"}
    assert "humidity" in result["description_placeholders"]["details"].lower()


def test_global_outdoor_temperature_required() -> None:
    errors, placeholders = validate_global(settings.defaults(), [])
    assert errors == {"base": "invalid_settings"}
    assert "Outdoor temperature" in placeholders["details"]
    options = settings.defaults() | {"outdoor_weather_entity": "weather.home"}
    assert validate_global(options, []) == ({}, {})
    options = settings.defaults() | {
        "outdoor_temperature_sensor_entity": "sensor.outdoor_temperature"
    }
    assert validate_global(options, []) == ({}, {})


@pytest.mark.parametrize(
    "shared_change,override",
    [
        ({"comfort_upper_temperature": 20}, {"comfort_lower_temperature": 21}),
        ({"airing_time_upper_limit": 6}, {"airing_time_lower_limit": 7}),
        ({"airing_time_lower_limit": 29}, {"airing_time_upper_limit": 28}),
    ],
)
def test_global_save_blocks_invalid_effective_room(
    shared_change: dict[str, Any], override: dict[str, Any]
) -> None:
    options = global_options(**shared_change)
    assert settings.validate_effective(options) == {}
    errors, placeholders = validate_global(options, [room_data(overrides=override)])
    assert errors == {"base": "invalid_settings"}
    assert "Bedroom" in placeholders["details"]


def test_sparse_overrides_ignore_global_only_and_restore_inheritance() -> None:
    shared = settings.global_settings(global_options())
    parsed = parse_overrides(
        {
            "comfort": {
                "comfort_lower_temperature_mode": "shared",
                "comfort_lower_temperature": 22,
                "comfort_upper_temperature_mode": "custom",
                "comfort_upper_temperature": 26,
            },
            "advanced": {"condition_delay_mode": "custom", "condition_delay": 0},
            "outdoor": {"outdoor_temperature_sensor_entity": "sensor.local"},
        },
        shared,
        {"comfort_lower_temperature": 22},
    )
    assert parsed == {"comfort_upper_temperature": 26.0}
    effective, sources = settings.merge(shared, parsed)
    assert sources["comfort_lower_temperature"] == "global"
    assert effective["comfort_lower_temperature"] == 19


def test_required_room_temperature_marker_and_validation(hass: HomeAssistant) -> None:
    from custom_components.ventilation_advisor.config_flow import (
        VentilationAdvisorConfigFlow,
    )

    fields = {str(marker): marker for marker in _room_schema({}, {}).schema}
    assert fields["indoor_temperature_entity"].__class__.__name__ == "Required"
    assert fields["indoor_humidity_entity"].__class__.__name__ == "Optional"
    flow = VentilationAdvisorConfigFlow()
    assert flow._validate_room(
        {
            key: value
            for key, value in ROOM_INPUT.items()
            if key != "indoor_temperature_entity"
        }
    ) == {"indoor_temperature_entity": "entity_required"}
    assert flow._validate_room(
        ROOM_INPUT | {"thermostat_entity": "sensor.temperature"}
    ) == {"thermostat_entity": "invalid_entity"}


async def test_options_flow_matches_setup_form(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(hass)
    setup = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert setup["type"] is FlowResultType.ABORT
    assert setup["reason"] == "single_instance_allowed"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("step_id") == "init", result

    def keys(schema) -> dict[str, set[str]]:
        return {
            str(section): {str(k) for k in value.schema.schema}
            for section, value in schema.schema.items()
        }

    assert keys(result["data_schema"]) == keys(global_schema(hass, {}))

    changed = global_options(notify_actions=["notify.phone"])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], sectioned(changed)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["notify_actions"] == ["notify.phone"]


async def test_options_flow_validates_room_overrides(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(
        hass,
        rooms=[room_data(overrides={"comfort_lower_humidity": 50.0})],
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    changed = global_options(comfort_upper_humidity=48.0)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], sectioned(changed)
    )
    assert result["errors"] == {"base": "invalid_settings"}
    assert "Bedroom" in result["description_placeholders"]["details"]


def test_conditional_goal_toggles_and_advanced_sections(hass: HomeAssistant) -> None:
    shared = settings.global_settings({})
    global_form = global_schema(hass, {})
    room_form = _overrides_schema(hass, {}, shared)
    for form in (global_form, room_form):
        sections = {str(k): v for k, v in form.schema.items()}
        for scenario in settings.SCENARIOS:
            section = sections[scenario]
            assert not section.options["collapsed"]
            close = next(
                k
                for k in section.schema.schema
                if str(k) == f"{scenario}_goal_reached_notification_enabled"
            )
            assert close.visible["field"] == f"{scenario}_notification_enabled"
        assert not sections["comfort"].options["collapsed"]
        assert sections["advanced"].options["collapsed"]
        assert sections["airing_time_advanced"].options["collapsed"]
        assert not sections["notifications"].options["collapsed"]
    fields = {
        str(k): k
        for k in room_form.schema[
            next(k for k in room_form.schema if str(k) == "drying_possible")
        ].schema.schema
    }
    assert fields["drying_possible_goal_reached_notification_enabled"].visible[
        "value"
    ] == [
        "on",
        "shared",
    ]
    shared["drying_possible_notification_enabled"] = False
    fields = {
        str(k): k
        for k in _overrides_schema(hass, {}, shared)
        .schema[next(k for k in room_form.schema if str(k) == "drying_possible")]
        .schema.schema
    }
    assert fields["drying_possible_goal_reached_notification_enabled"].visible[
        "value"
    ] == ["on"]
    assert "area_id" not in {str(k) for k in _room_schema({}, shared).schema}
    assert "location_phrase" not in {str(k) for k in _room_schema({}, shared).schema}


def test_airing_interval_selector_has_no_maximum(hass: HomeAssistant) -> None:
    for form in (
        global_schema(hass, {}),
        _overrides_schema(hass, {}, settings.defaults()),
    ):
        section = next(
            value for key, value in form.schema.items() if str(key) == "overdue_airing"
        )
        selector = next(
            value
            for key, value in section.schema.schema.items()
            if str(key) == "airing_interval"
        )
        assert selector.config["min"] == 1
        assert selector.config.get("max") is None
        assert selector(10_000) == 10_000


@pytest.mark.parametrize("key", ["window_open_debounce", "window_close_debounce"])
def test_window_delay_selector_allows_single_seconds(
    hass: HomeAssistant, key: str
) -> None:
    section = next(
        value
        for marker, value in global_schema(hass, {}).schema.items()
        if str(marker) == "advanced"
    )
    selector = next(
        value for marker, value in section.schema.schema.items() if str(marker) == key
    )
    assert selector.config["step"] == 1
    assert selector(7) == 7
    assert settings.validate_value(settings.SETTINGS_BY_KEY[key], 7) is None


def test_source_selectors_use_device_classes(hass: HomeAssistant) -> None:
    shared = settings.global_settings({})
    fields = {str(k): v for k, v in _room_schema({}, shared).schema.items()}
    assert fields["contacts"].config["multiple"] is True
    assert fields["contacts"].config["filter"] == [
        {
            "domain": ["binary_sensor"],
            "device_class": ["opening", "door", "window", "garage_door"],
        }
    ]
    assert fields["thermostat_entity"].config["filter"] == [{"domain": ["climate"]}]
    outdoor_section = next(
        value
        for key, value in global_schema(hass, {}).schema.items()
        if str(key) == "outdoor"
    )
    global_fields = {str(k): v for k, v in outdoor_section.schema.schema.items()}
    for kind in ("temperature", "humidity"):
        indoor = f"indoor_{kind}_entity"
        outdoor = f"outdoor_{kind}_sensor_entity"
        assert fields[indoor].config["filter"] == [
            {"domain": ["sensor"], "device_class": [kind]}
        ]
        assert outdoor not in fields
        assert f"{outdoor}_mode" not in fields
        assert global_fields[outdoor].config["filter"] == [
            {"domain": ["sensor"], "device_class": [kind]}
        ]
    assert global_fields["outdoor_weather_entity"].config["filter"] == [
        {"domain": ["weather"]}
    ]


def test_legacy_notify_choices(hass: HomeAssistant) -> None:
    for name in (
        "send_message",
        "persistent_notification",
        "mobile_app_phone",
        "family",
    ):
        hass.services.async_register("notify", name, lambda call: None)
    assert _notify_options(
        hass,
        ["notify.old_phone", "notify.send_message", "notify.persistent_notification"],
    ) == ["notify.family", "notify.mobile_app_phone", "notify.old_phone"]


@pytest.mark.parametrize(
    "action", ["notify.send_message", "notify.persistent_notification"]
)
async def test_setup_rejects_unsupported_custom_notify_action(
    hass: HomeAssistant, action: str
) -> None:
    result = await _start(hass, global_options(notify_actions=[action]))
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_settings"}
    assert action in result["description_placeholders"]["details"]


@pytest.mark.parametrize(
    "action", ["notify.send_message", "notify.persistent_notification"]
)
async def test_options_explains_existing_unsupported_notify_action(
    hass: HomeAssistant, action: str
) -> None:
    entry = await setup_entry(
        hass, options=global_options(notify_actions=[action]), rooms=[]
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "invalid_settings"}
    assert action in result["description_placeholders"]["details"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], sectioned(global_options(notify_actions=[action]))
    )
    assert result["errors"] == {"base": "invalid_settings"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], sectioned(global_options(notify_actions=["notify.family"]))
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["notify_actions"] == ["notify.family"]


def test_hidden_goal_and_global_room_conflict() -> None:
    options = sectioned(global_options())
    options["drying_possible"]["drying_possible_notification_enabled"] = False
    options["drying_possible"].pop("drying_possible_goal_reached_notification_enabled")
    flat = flatten_global(options)
    assert flat["drying_possible_goal_reached_notification_enabled"] is False
    room = room_data(
        overrides={"drying_possible_goal_reached_notification_enabled": True}
    )
    errors, placeholders = validate_global(flat, [room])
    assert errors == {"base": "invalid_settings"}
    assert room["name"] in placeholders["details"]
    parsed = parse_overrides(
        {"drying_possible": {"drying_possible_notification_enabled": "off"}},
        settings.global_settings({}),
    )
    assert parsed == {
        "drying_possible_notification_enabled": False,
        "drying_possible_goal_reached_notification_enabled": False,
    }
    shared = settings.global_settings(
        {
            "drying_possible_notification_enabled": False,
            "drying_possible_goal_reached_notification_enabled": False,
        }
    )
    existing = {"drying_possible_goal_reached_notification_enabled": False}
    assert parse_overrides({}, shared, existing) == existing
    assert parse_overrides({}, shared, {}) == {}


def test_disabling_room_notification_normalizes_stale_goal_on() -> None:
    shared = settings.global_settings(
        {
            "drying_possible_goal_reached_notification_enabled": False,
        }
    )
    existing = {
        "drying_possible_notification_enabled": True,
        "drying_possible_goal_reached_notification_enabled": True,
    }
    parsed = parse_overrides(
        {
            "drying_possible": {
                "drying_possible_notification_enabled": "off",
                "drying_possible_goal_reached_notification_enabled": "on",
            }
        },
        shared,
        existing,
    )
    assert parsed == {
        "drying_possible_notification_enabled": False,
        "drying_possible_goal_reached_notification_enabled": False,
    }
    effective, _ = settings.merge(shared, parsed)
    assert settings.validate_effective(effective) == {}


async def test_hidden_room_goal_gets_safe_override(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(hass, rooms=[])
    result = await _room_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], ROOM_INPUT
    )
    result = await configure_subentry(
        hass,
        result,
        {"drying_possible": {"drying_possible_notification_enabled": "off"}},
    )
    assert result["step_id"] == "review"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    room = next(iter(entry.subentries.values()))
    assert room.data["overrides"] == {
        "drying_possible_notification_enabled": False,
        "drying_possible_goal_reached_notification_enabled": False,
    }


def test_review_profile_and_inheritance(hass: HomeAssistant) -> None:
    summary = review_summary(
        hass, {}, room_data(overrides={"reference_airing_time": 10})
    )
    assert "| 16 °C | 5 K | 14.1 min | 21.2 min |" in summary
    assert "Reference airing time: 10 min (room)" in summary
    assert "all advice deferred" in summary
    assert "max(comfort lower, setpoint)" in summary
    assert "one quarter" in summary


async def _room_flow(hass: HomeAssistant, entry, source: str = "user", **kw):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": source, **kw}
    )


async def test_add_room_validation(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(hass)
    result = await _room_flow(hass, entry)
    assert result["step_id"] == "room"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ROOM_INPUT, "name": " bedroom "}
    )
    assert result["errors"]["name"] == "duplicate_name"
    assert result["errors"]["contacts"] == "contact_in_use"

    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {
                **ROOM_INPUT,
                "name": "Office",
                "contacts": ["binary_sensor.office_window"],
                "indoor_temperature_entity": "",
            },
        )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **{
                key: value
                for key, value in ROOM_INPUT.items()
                if key != "indoor_humidity_entity"
            },
            "name": "Office",
            "contacts": ["binary_sensor.office_window"],
        },
    )
    assert result["step_id"] == "overrides"
    result = await configure_subentry(
        hass,
        result,
        {
            "comfort": {
                "comfort_upper_humidity_mode": "custom",
                "comfort_upper_humidity": 70,
            },
            "cooling_possible": {"cooling_possible_notification_enabled": "off"},
            "quiet_hours": {
                "quiet_hours_times_mode": "custom",
                "quiet_hours_start": "23:30",
                "quiet_hours_end": "06:00",
            },
        },
    )
    assert result["step_id"] == "review"
    assert (
        "Drying possible: not configured"
        in result["description_placeholders"]["summary"]
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    office = next(s for s in entry.subentries.values() if s.title == "Office")
    assert office.data["overrides"] == {
        "comfort_upper_humidity": 70.0,
        "cooling_possible_notification_enabled": False,
        "cooling_possible_goal_reached_notification_enabled": False,
        "quiet_hours_start": "23:30:00",
        "quiet_hours_end": "06:00:00",
    }


async def test_overrides_reject_invalid_combination(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(hass, rooms=[])
    result = await _room_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], ROOM_INPUT
    )
    result = await configure_subentry(
        hass,
        result,
        {
            "comfort": {
                "comfort_lower_humidity_mode": "custom",
                "comfort_lower_humidity": 80,
            }
        },
    )
    assert result["step_id"] == "overrides"
    assert result["errors"] == {"base": "invalid_settings"}


async def test_reconfigure_room_keeps_identity(hass: HomeAssistant) -> None:
    states(hass)
    entry = await setup_entry(
        hass,
        rooms=[
            room_data(
                location_phrase="in the bedroom",
                overrides={"heat_loss_notification_enabled": False},
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))
    result = await _room_flow(
        hass, entry, source=config_entries.SOURCE_RECONFIGURE, subentry_id=subentry_id
    )
    assert result["step_id"] == "room"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ROOM_INPUT, "name": "Main bedroom"}
    )
    assert result["step_id"] == "overrides"
    # The stored override is preselected.
    scenarios = next(
        v for k, v in result["data_schema"].schema.items() if str(k) == "heat_loss"
    )
    defaults = {str(k): k.default() for k in scenarios.schema.schema}
    assert defaults["heat_loss_notification_enabled"] == "off"
    result = await configure_subentry(
        hass, result, {"heat_loss": {"heat_loss_notification_enabled": "shared"}}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Main bedroom"
    assert subentry.data["room_id"] == "room1"
    assert subentry.data["overrides"] == {}
    assert "location_phrase" not in subentry.data


async def test_every_form_field_is_translated(hass: HomeAssistant) -> None:
    import json
    from pathlib import Path

    from custom_components.ventilation_advisor import config_flow

    strings = json.loads(
        Path("custom_components/ventilation_advisor/translations/en.json").read_text(
            encoding="utf-8"
        )
    )

    def check(schema, step: dict[str, Any], where: str) -> None:
        for marker, value in schema.schema.items():
            key = str(marker)
            nested = getattr(value, "schema", None)
            if nested is not None and hasattr(nested, "schema"):
                section = step["sections"][key]
                assert section.get("name"), f"{where}.{key}"
                check(nested, section, f"{where}.{key}")
            else:
                assert key in step.get("data", {}), f"{where}.{key}"

    shared = settings.global_settings(global_options())
    check(
        config_flow.global_schema(hass, {}), strings["config"]["step"]["user"], "user"
    )
    check(
        config_flow.global_schema(hass, {}), strings["options"]["step"]["init"], "init"
    )
    room_steps = strings["config_subentries"]["room"]["step"]
    check(config_flow._room_schema({}, shared), room_steps["room"], "room")
    check(
        config_flow._overrides_schema(hass, {}, shared),
        room_steps["overrides"],
        "overrides",
    )
    check(
        config_flow._room_schema({}, shared),
        strings["config"]["step"]["room"],
        "c.room",
    )
