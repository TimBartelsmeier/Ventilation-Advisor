"""Config, options, and room subentry flows.

The global form is shared by initial setup and the options flow; the room
wizard (room -> overrides -> review) is shared by setup, adding, and editing a
room. All fields derive from ``settings.SETTINGS``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_ADVISOR_ENABLED,
    CONF_CONTACTS,
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMPERATURE,
    CONF_NAME,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_TEMPERATURE,
    CONF_OUTDOOR_WEATHER,
    CONF_OVERRIDES,
    CONF_ROOM_ID,
    CONF_THERMOSTAT,
    DOMAIN,
    SUBENTRY_ROOM,
)
from .settings import (
    LANGUAGES,
    PROTECT_SCENARIOS,
    QUIET_GROUP,
    SCENARIOS,
    SECTION_LABELS,
    SECTIONS,
    SETTINGS,
    SETTINGS_BY_KEY,
    Kind,
    Scope,
    Setting,
    airing_duration_s,
    global_settings,
    merge,
    validate_effective,
)

MODE_SHARED = "shared"
MODE_CUSTOM = "custom"
MODE_ON = "on"
MODE_OFF = "off"
MODE_SUFFIX = "_mode"
QUIET_KEYS = ("quiet_hours_start", "quiet_hours_end")
OUTDOOR_KEYS = (
    CONF_OUTDOOR_TEMPERATURE,
    CONF_OUTDOOR_HUMIDITY,
    CONF_OUTDOOR_WEATHER,
)
EXPANDED_GLOBAL_SECTIONS = (
    "outdoor",
    "comfort",
    "airing_time",
    *SCENARIOS,
    *PROTECT_SCENARIOS,
    "notifications",
)
PROFILE_DIFFERENCES = (25.0, 10.0, 5.0, 2.0, 0.0)
MAX_NAME_LENGTH = 80
UNSUPPORTED_NOTIFY_ACTIONS = {"notify.send_message", "notify.persistent_notification"}


# --------------------------------------------------------------------------
# Selectors


def _notify_options(hass: HomeAssistant, current: list[str]) -> list[str]:
    services = [
        f"notify.{name}" for name in hass.services.async_services_for_domain("notify")
    ]
    return sorted((set(services) | set(current)) - UNSUPPORTED_NOTIFY_ACTIONS)


def _entity_selector(
    key: str, domains: tuple[str, ...], *, multiple: bool = False
) -> sel.EntitySelector:
    filters: list[sel.EntityWithDeviceFilterSelectorConfig] = []
    for domain in domains:
        entity_filter = sel.EntityWithDeviceFilterSelectorConfig(domain=domain)
        if domain == "sensor":
            entity_filter["device_class"] = (
                "humidity"
                if key in (CONF_INDOOR_HUMIDITY, CONF_OUTDOOR_HUMIDITY)
                else "temperature"
            )
        elif domain == "binary_sensor":
            entity_filter["device_class"] = [
                "opening",
                "door",
                "window",
                "garage_door",
            ]
        filters.append(entity_filter)
    return sel.EntitySelector(
        sel.EntitySelectorConfig(filter=filters, multiple=multiple)
    )


def _selector(hass: HomeAssistant, setting: Setting, current: Any) -> Any:
    match setting.kind:
        case Kind.BOOL:
            return sel.BooleanSelector()
        case Kind.NUMBER:
            config = sel.NumberSelectorConfig(
                step=setting.step or "any", mode=sel.NumberSelectorMode.BOX
            )
            if setting.minimum is not None:
                config["min"] = setting.minimum
            if setting.maximum is not None:
                config["max"] = setting.maximum
            if setting.unit:
                config["unit_of_measurement"] = setting.unit
            return sel.NumberSelector(config)
        case Kind.TIME:
            return sel.TimeSelector()
        case Kind.ENTITY:
            return _entity_selector(setting.key, setting.entity_domains)
        case Kind.NOTIFY_LIST:
            return sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=_notify_options(hass, list(current or [])),
                    multiple=True,
                    custom_value=True,
                    mode=sel.SelectSelectorMode.DROPDOWN,
                )
            )
        case Kind.LANGUAGE:
            return sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=list(LANGUAGES), translation_key="language"
                )
            )
    raise ValueError(setting.kind)  # pragma: no cover


def _mode_selector(*modes: str) -> sel.SelectSelector:
    return sel.SelectSelector(
        sel.SelectSelectorConfig(
            options=list(modes),
            translation_key="override_mode",
            mode=sel.SelectSelectorMode.DROPDOWN,
        )
    )


def _normalize(setting: Setting, value: Any) -> Any:
    """Normalize selector output to stored types."""
    if setting.kind is Kind.NUMBER and isinstance(value, (int, float)):
        return float(value)
    if setting.kind is Kind.TIME and isinstance(value, str) and value.count(":") == 1:
        return f"{value}:00"
    if setting.kind is Kind.ENTITY and value in ("", None):
        return None
    if setting.kind is Kind.NOTIFY_LIST:
        return list(dict.fromkeys(value or []))
    return value


# --------------------------------------------------------------------------
# Global form (setup and options)


def global_schema(hass: HomeAssistant, options: Mapping[str, Any]) -> probatio.Schema:
    """Build the sectioned global settings form prefilled with ``options``."""
    current = global_settings(options)
    sections: dict[Any, Any] = {}
    for section_id in SECTIONS:
        fields: dict[Any, Any] = {}
        for setting in SETTINGS:
            if setting.section != section_id:
                continue
            value = current[setting.key]
            if setting.kind is Kind.ENTITY:
                marker = probatio.Optional(
                    setting.key,
                    description={"suggested_value": value} if value else None,
                )
            elif setting.key.endswith("_goal_reached_notification_enabled"):
                open_key = setting.key.replace(
                    "_goal_reached_notification_enabled", "_notification_enabled"
                )
                marker = data_entry_flow.Required(
                    setting.key,
                    default=value,
                    visible={"field": open_key, "value": True},
                )
            else:
                marker = probatio.Required(setting.key, default=value)
            fields[marker] = _selector(hass, setting, value)
        sections[probatio.Required(section_id)] = data_entry_flow.section(
            probatio.Schema(fields),
            {"collapsed": section_id not in EXPANDED_GLOBAL_SECTIONS},
        )
    return probatio.Schema(sections)


def flatten_global(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten sectioned input into stored options."""
    options: dict[str, Any] = {}
    for section_id in SECTIONS:
        values = user_input.get(section_id) or {}
        for setting in SETTINGS:
            if setting.section != section_id:
                continue
            if setting.key.endswith("_goal_reached_notification_enabled"):
                open_key = setting.key.replace(
                    "_goal_reached_notification_enabled", "_notification_enabled"
                )
                if not values.get(open_key):
                    options[setting.key] = False
                    continue
            options[setting.key] = _normalize(setting, values.get(setting.key))
    return options


def validate_global(
    options: Mapping[str, Any], rooms: list[Mapping[str, Any]]
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate global options and all rooms' merged settings.

    Returns ``(errors, placeholders)``.
    """
    details: list[str] = []
    details.extend(_validation_details(global_settings(options)))
    if not (options.get(CONF_OUTDOOR_TEMPERATURE) or options.get(CONF_OUTDOOR_WEATHER)):
        details.append(
            "Outdoor temperature: select a temperature sensor or weather entity."
        )
    for room in rooms:
        effective, _ = merge(options, room.get(CONF_OVERRIDES) or {})
        details.extend(
            f"{room[CONF_NAME]} – {detail}" for detail in _validation_details(effective)
        )
    if not details:
        return {}, {}
    return {"base": "invalid_settings"}, {"details": "\n".join(details)}


def _validation_details(effective: Mapping[str, Any]) -> list[str]:
    details: list[str] = []
    for key, error in validate_effective(effective).items():
        text = _error_text(error)
        if key == "notify_actions":
            actions = effective.get(key)
            if isinstance(actions, (list, tuple)):
                unsupported = [
                    action
                    for action in actions
                    if isinstance(action, str) and action in UNSUPPORTED_NOTIFY_ACTIONS
                ]
                if unsupported:
                    text = (
                        f"Unsupported actions: {', '.join(unsupported)}. "
                        "Modern notify entities require entity targets and are not "
                        "supported. Choose legacy notify actions or enable Home "
                        "Assistant notifications for persistent advice."
                    )
        details.append(f"{_label(key)}: {text}")
    return details


def _label(key: str) -> str:
    setting = SETTINGS_BY_KEY.get(key)
    if not setting:
        return key
    section = setting.section.removesuffix("_advanced")
    if section in (*SCENARIOS, *PROTECT_SCENARIOS):
        return f"{SECTION_LABELS[section]} – {setting.label}"
    return setting.label


def _error_text(error: str) -> str:
    if error.startswith("must_be_below_"):
        return f"must be below {_label(error.removeprefix('must_be_below_'))}"
    if error.startswith("must_be_at_most_"):
        return f"must be at most {_label(error.removeprefix('must_be_at_most_'))}"
    if error.startswith("requires_"):
        return f"requires {_label(error.removeprefix('requires_'))} to be on"
    return error.replace("_", " ")


# --------------------------------------------------------------------------
# Room wizard


def _room_schema(data: Mapping[str, Any], shared: Mapping[str, Any]) -> probatio.Schema:
    fields: dict[Any, Any] = {
        probatio.Required(
            CONF_NAME, default=data.get(CONF_NAME, "")
        ): sel.TextSelector(),
        probatio.Required(
            CONF_ADVISOR_ENABLED, default=data.get(CONF_ADVISOR_ENABLED, True)
        ): sel.BooleanSelector(),
        probatio.Required(
            CONF_CONTACTS, default=list(data.get(CONF_CONTACTS) or [])
        ): _entity_selector(CONF_CONTACTS, ("binary_sensor",), multiple=True),
        probatio.Required(
            CONF_INDOOR_TEMPERATURE,
            default=data.get(CONF_INDOOR_TEMPERATURE),
        ): _entity_selector(CONF_INDOOR_TEMPERATURE, ("sensor",)),
        probatio.Optional(
            CONF_INDOOR_HUMIDITY,
            description={"suggested_value": data.get(CONF_INDOOR_HUMIDITY)},
        ): _entity_selector(CONF_INDOOR_HUMIDITY, ("sensor",)),
        probatio.Optional(
            CONF_THERMOSTAT,
            description={"suggested_value": data.get(CONF_THERMOSTAT)},
        ): _entity_selector(CONF_THERMOSTAT, ("climate",)),
    }
    return probatio.Schema(fields)


def _overrides_schema(
    hass: HomeAssistant, overrides: Mapping[str, Any], shared: Mapping[str, Any]
) -> probatio.Schema:
    sections: dict[Any, Any] = {}
    for section_id in SECTIONS:
        fields: dict[Any, Any] = {}
        quiet_added = False
        for setting in SETTINGS:
            if (
                setting.section != section_id
                or setting.scope is not Scope.SHARED
                or setting.key in OUTDOOR_KEYS
            ):
                continue
            key = setting.key
            if setting.kind is Kind.BOOL:
                mode = (
                    MODE_SHARED
                    if key not in overrides
                    else (MODE_ON if overrides[key] else MODE_OFF)
                )
                marker: Any = probatio.Required(key, default=mode)
                if key.endswith("_goal_reached_notification_enabled"):
                    open_key = key.replace(
                        "_goal_reached_notification_enabled", "_notification_enabled"
                    )
                    visible_modes = [MODE_ON]
                    if shared[open_key]:
                        visible_modes.append(MODE_SHARED)
                    marker = data_entry_flow.Required(
                        key,
                        default=mode,
                        visible={
                            "field": open_key,
                            "operator": "in",
                            "value": visible_modes,
                        },
                    )
                fields[marker] = _mode_selector(MODE_SHARED, MODE_ON, MODE_OFF)
                continue
            if setting.group == QUIET_GROUP:
                if not quiet_added:
                    custom = any(k in overrides for k in QUIET_KEYS)
                    fields[
                        probatio.Required(
                            f"{QUIET_GROUP}{MODE_SUFFIX}",
                            default=MODE_CUSTOM if custom else MODE_SHARED,
                        )
                    ] = _mode_selector(MODE_SHARED, MODE_CUSTOM)
                    quiet_added = True
                fields[
                    data_entry_flow.Required(
                        key,
                        default=overrides.get(key, shared[key]),
                        visible={
                            "field": f"{QUIET_GROUP}{MODE_SUFFIX}",
                            "value": MODE_CUSTOM,
                        },
                    )
                ] = _selector(hass, setting, None)
                continue
            fields[
                probatio.Required(
                    f"{key}{MODE_SUFFIX}",
                    default=MODE_CUSTOM if key in overrides else MODE_SHARED,
                )
            ] = _mode_selector(MODE_SHARED, MODE_CUSTOM)
            fields[
                data_entry_flow.Required(
                    key,
                    default=overrides.get(key, shared[key]),
                    visible={"field": f"{key}{MODE_SUFFIX}", "value": MODE_CUSTOM},
                )
            ] = _selector(hass, setting, None)
        if fields:
            sections[probatio.Required(section_id)] = data_entry_flow.section(
                probatio.Schema(fields),
                {"collapsed": section_id not in EXPANDED_GLOBAL_SECTIONS},
            )
    return probatio.Schema(sections)


def parse_overrides(
    user_input: Mapping[str, Any],
    shared: Mapping[str, Any] | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert the overrides form into sparse room overrides."""
    result: dict[str, Any] = {}
    for section_id in SECTIONS:
        values = user_input.get(section_id) or {}
        for setting in SETTINGS:
            if setting.section != section_id or setting.scope is not Scope.SHARED:
                continue
            key = setting.key
            if key in OUTDOOR_KEYS:
                continue
            if setting.kind is Kind.BOOL:
                mode = values.get(key, MODE_SHARED)
                if mode in (MODE_ON, MODE_OFF):
                    result[key] = mode == MODE_ON
            elif setting.group == QUIET_GROUP:
                if values.get(f"{QUIET_GROUP}{MODE_SUFFIX}") == MODE_CUSTOM:
                    result[key] = _normalize(setting, values.get(key))
            elif values.get(f"{key}{MODE_SUFFIX}") == MODE_CUSTOM:
                result[key] = _normalize(setting, values.get(key))
    if shared is not None:
        for prefix in SCENARIOS:
            open_key = f"{prefix}_notification_enabled"
            close_key = f"{prefix}_goal_reached_notification_enabled"
            if not result.get(open_key, shared[open_key]) and (
                result.get(close_key, shared[close_key])
                or (existing or {}).get(close_key) is False
            ):
                result[close_key] = False
    return result


def _fmt(value: Any, setting: Setting | None) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "none"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, list):
        return ", ".join(value) or "none"
    unit = f" {setting.unit}" if setting and setting.unit else ""
    return f"{value}{unit}"


def _scenario_status(
    effective: Mapping[str, Any], room: Mapping[str, Any]
) -> list[str]:
    indoor_t = bool(room.get(CONF_INDOOR_TEMPERATURE))
    indoor_h = bool(room.get(CONF_INDOOR_HUMIDITY))
    weather = bool(effective.get(CONF_OUTDOOR_WEATHER))
    outdoor_t = bool(effective.get(CONF_OUTDOOR_TEMPERATURE) or weather)
    outdoor_h = bool(effective.get(CONF_OUTDOOR_HUMIDITY) or weather)
    temperature_missing = [
        n
        for n, ok in (
            ("indoor temperature", indoor_t),
            ("outdoor temperature", outdoor_t),
        )
        if not ok
    ]
    humidity_missing = temperature_missing + [
        n
        for n, ok in (("indoor humidity", indoor_h), ("outdoor humidity", outdoor_h))
        if not ok
    ]
    lines = []
    for scenario in (*SCENARIOS, *PROTECT_SCENARIOS):
        key = f"{scenario}_notification_enabled"
        label = SECTION_LABELS[scenario]
        missing = (
            []
            if scenario == "overdue_airing"
            else humidity_missing
            if scenario
            in (
                "drying_possible",
                "humidifying_possible",
                "moisture_loss",
                "moisture_gain",
            )
            else temperature_missing
        )
        if not effective[key]:
            status = "disabled"
        elif missing:
            status = f"not configured (missing {', '.join(missing)})"
        else:
            status = "enabled"
        lines.append(f"- {label}: {status}")
    return lines


def review_summary(
    hass: HomeAssistant, options: Mapping[str, Any], room: Mapping[str, Any]
) -> str:
    """Render the review screen summary (Markdown)."""
    overrides = room.get(CONF_OVERRIDES) or {}
    effective, sources = merge(options, overrides)
    lines = [f"**{room[CONF_NAME]}**", ""]
    if not room.get(CONF_ADVISOR_ENABLED, True):
        lines += ["Advisor is **paused** for this room.", ""]
    lines.append("**Sources**")
    lines.append(f"- Contacts: {', '.join(room[CONF_CONTACTS])}")
    for key, label in (
        (CONF_INDOOR_TEMPERATURE, "Indoor temperature"),
        (CONF_INDOOR_HUMIDITY, "Indoor humidity"),
        (CONF_THERMOSTAT, "Climate (heating-only)"),
    ):
        lines.append(f"- {label}: {room.get(key) or 'none'}")
    for key in OUTDOOR_KEYS:
        lines.append(
            f"- {_label(key)}: {effective.get(key) or 'none'} ({sources[key]})"
        )
    lines += ["", "**Scenarios**", *_scenario_status(effective, room)]
    lines += [
        "",
        "**Comfort window**",
        f"- Temperature: {effective['comfort_lower_temperature']:g}–"
        f"{effective['comfort_upper_temperature']:g} °C",
        f"- Humidity: {effective['comfort_lower_humidity']:g}–"
        f"{effective['comfort_upper_humidity']:g} %RH",
        "- Heating raises the lower temperature to max(comfort lower, setpoint), "
        "and the upper to max(comfort upper, effective lower + 1 °C). "
        "Humidity is judged at max(indoor temperature, heating setpoint).",
        "- Fixed hysteresis: 0.5 °C / 3 %RH, capped at one quarter of the window width.",
        "- Opening goals are independent. Reaching all enabled goals permits closing; "
        "adverse-effect close suggestions apply only after the maximum airing time.",
    ]

    lines += ["", "**Notifications**"]
    targets = []
    if not effective["notifications_enabled"]:
        targets.append("disabled")
    else:
        if effective["persistent_notifications_enabled"]:
            targets.append("Home Assistant notifications")
        targets.extend(effective["notify_actions"])
    lines.append(f"- Destinations: {', '.join(targets) or 'none'}")
    if effective["quiet_hours_enabled"]:
        lines.append(
            f"- Quiet hours: {effective['quiet_hours_start'][:5]}–"
            f"{effective['quiet_hours_end'][:5]} (all advice deferred)"
        )
    else:
        lines.append("- Quiet hours: off")

    lines += [
        "",
        "**Airing time profile** (21 °C inside)",
        "| Outdoor | ΔT | Recommended | Maximum |",
        "| --- | --- | --- | --- |",
    ]
    for difference in PROFILE_DIFFERENCES:
        recommended = airing_duration_s(effective, "recommended", difference) / 60
        maximum = airing_duration_s(effective, "maximum", difference) / 60
        lines.append(
            f"| {21 - difference:g} °C | {difference:g} K | {recommended:.1f} min | {maximum:.1f} min |"
        )
    lines.append(
        "Both times are fixed at confirmed opening; missing temperatures use the upper recommended limit."
    )

    effective_values = [
        f"- {_label(key)}: {_fmt(effective[key], SETTINGS_BY_KEY.get(key))} ({sources[key]})"
        for key in effective
        if SETTINGS_BY_KEY[key].scope is Scope.SHARED
    ]
    lines += ["", "**Effective settings**", *effective_values]
    if not any(source == "room" for source in sources.values()):
        lines.append("Room overrides: none.")

    warnings = []
    if (
        effective["notifications_enabled"]
        and not effective["persistent_notifications_enabled"]
        and not effective["notify_actions"]
    ):
        warnings.append("No notification destination: advice cannot be delivered.")
    bound = [
        *room[CONF_CONTACTS],
        room.get(CONF_INDOOR_TEMPERATURE),
        room.get(CONF_INDOOR_HUMIDITY),
        room.get(CONF_THERMOSTAT),
        *(effective.get(k) for k in OUTDOOR_KEYS),
    ]
    for entity_id in bound:
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            warnings.append(f"{entity_id} is currently unavailable.")
    if warnings:
        lines += ["", "**Warnings**", *(f"- {w}" for w in warnings)]
    return "\n".join(lines)


class RoomWizard:
    """Mixin implementing the room wizard steps (room -> overrides -> review)."""

    hass: HomeAssistant
    _room: dict[str, Any]

    def _wizard_options(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def _other_rooms(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    async def _async_finish_room(self, room: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _start_wizard(self, room: Mapping[str, Any] | None) -> None:
        self._room = dict(room or {})
        self._room.pop("location_phrase", None)
        self._room.setdefault(CONF_ROOM_ID, uuid.uuid4().hex)
        self._room.setdefault(CONF_OVERRIDES, {})

    def _validate_room(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        name = (user_input.get(CONF_NAME) or "").strip()
        others = self._other_rooms()
        if not name or len(name) > MAX_NAME_LENGTH:
            errors[CONF_NAME] = "invalid_name"
        elif any(r[CONF_NAME].strip().casefold() == name.casefold() for r in others):
            errors[CONF_NAME] = "duplicate_name"
        contacts = list(dict.fromkeys(user_input.get(CONF_CONTACTS) or []))
        used = {c for r in others for c in r[CONF_CONTACTS]}
        if not contacts:
            errors[CONF_CONTACTS] = "contacts_required"
        elif used.intersection(contacts):
            errors[CONF_CONTACTS] = "contact_in_use"
        if not user_input.get(CONF_INDOOR_TEMPERATURE):
            errors[CONF_INDOOR_TEMPERATURE] = "entity_required"
        for key, domains in (
            (CONF_CONTACTS, ("binary_sensor",)),
            (CONF_INDOOR_TEMPERATURE, ("sensor",)),
            (CONF_INDOOR_HUMIDITY, ("sensor",)),
            (CONF_THERMOSTAT, ("climate",)),
        ):
            values = contacts if key == CONF_CONTACTS else [user_input.get(key)]
            for value in values:
                if value and (
                    not isinstance(value, str)
                    or "." not in value
                    or value.split(".", 1)[0] not in domains
                    or not value.split(".", 1)[1]
                ):
                    errors[key] = "invalid_entity"
        return errors

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> Any:
        """Identity, contacts, and sensors."""
        errors: dict[str, str] = {}
        shared = global_settings(self._wizard_options())
        if user_input is not None:
            errors = self._validate_room(user_input)
            if not errors:
                for key in (
                    CONF_INDOOR_TEMPERATURE,
                    CONF_INDOOR_HUMIDITY,
                    CONF_THERMOSTAT,
                ):
                    self._room[key] = user_input.get(key) or None
                self._room[CONF_NAME] = user_input[CONF_NAME].strip()
                self._room[CONF_ADVISOR_ENABLED] = user_input[CONF_ADVISOR_ENABLED]
                self._room[CONF_CONTACTS] = list(
                    dict.fromkeys(user_input[CONF_CONTACTS])
                )
                return await self.async_step_overrides()
            data = {**self._room, **user_input}
        else:
            data = self._room
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="room",
            data_schema=_room_schema(data, shared),
            errors=errors,
        )

    async def async_step_overrides(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Per-room overrides of shared settings."""
        options = self._wizard_options()
        shared = global_settings(options)
        errors: dict[str, str] = {}
        placeholders = {"details": ""}
        if user_input is not None:
            overrides = parse_overrides(
                user_input, shared, self._room.get(CONF_OVERRIDES)
            )
            effective, _ = merge(options, overrides)
            invalid = validate_effective(effective)
            if not invalid:
                self._room[CONF_OVERRIDES] = overrides
                return await self.async_step_review()
            errors["base"] = "invalid_settings"
            placeholders["details"] = "\n".join(_validation_details(effective))
            current = overrides
        else:
            current = {
                k: v
                for k, v in (self._room.get(CONF_OVERRIDES) or {}).items()
                if k not in OUTDOOR_KEYS
            }
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="overrides",
            data_schema=_overrides_schema(self.hass, current, shared),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_review(self, user_input: dict[str, Any] | None = None) -> Any:
        """Show the effective configuration and save."""
        if user_input is not None:
            return await self._async_finish_room(self._room)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="review",
            data_schema=probatio.Schema({}),
            description_placeholders={
                "summary": review_summary(self.hass, self._wizard_options(), self._room)
            },
        )


def _room_title(room: Mapping[str, Any]) -> str:
    return room[CONF_NAME]


def _rooms_of(entry: ConfigEntry) -> list[Mapping[str, Any]]:
    return [
        s.data for s in entry.subentries.values() if s.subentry_type == SUBENTRY_ROOM
    ]


# --------------------------------------------------------------------------
# Flows


class VentilationAdvisorConfigFlow(RoomWizard, ConfigFlow, domain=DOMAIN):
    """Initial setup: global settings, then an optional first room."""

    VERSION = 1

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._room = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return VentilationAdvisorOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {SUBENTRY_ROOM: RoomSubentryFlow}

    def _wizard_options(self) -> Mapping[str, Any]:
        return self._options

    def _other_rooms(self) -> list[Mapping[str, Any]]:
        return []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Global settings."""
        errors: dict[str, str] = {}
        placeholders = {"details": ""}
        options: Mapping[str, Any] = self._options
        if user_input is not None:
            options = flatten_global(user_input)
            errors, found = validate_global(options, [])
            placeholders |= found
            if not errors:
                self._options = options
                return await self.async_step_first_room()
        return self.async_show_form(
            step_id="user",
            data_schema=global_schema(self.hass, options),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_first_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer adding the first room now."""
        return self.async_show_menu(
            step_id="first_room", menu_options=["add_room", "finish"]
        )

    async def async_step_add_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the room wizard."""
        self._start_wizard(None)
        return await self.async_step_room()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry without rooms."""
        return self.async_create_entry(
            title="Ventilation Advisor", data={}, options=self._options
        )

    async def _async_finish_room(self, room: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title="Ventilation Advisor",
            data={},
            options=self._options,
            subentries=[
                {
                    "data": room,
                    "subentry_type": SUBENTRY_ROOM,
                    "title": _room_title(room),
                    "unique_id": room[CONF_ROOM_ID],
                }
            ],
        )


class VentilationAdvisorOptionsFlow(OptionsFlow):
    """Edit global settings (same form as setup)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Global settings."""
        errors: dict[str, str] = {}
        placeholders = {"details": ""}
        options: Mapping[str, Any] = self.config_entry.options
        if user_input is None and any(
            action in UNSUPPORTED_NOTIFY_ACTIONS
            for action in options.get("notify_actions", [])
            if isinstance(action, str)
        ):
            errors, found = validate_global(options, [])
            placeholders |= found
        if user_input is not None:
            options = flatten_global(user_input)
            errors, found = validate_global(options, _rooms_of(self.config_entry))
            placeholders |= found
            if not errors:
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=global_schema(self.hass, options),
            errors=errors,
            description_placeholders=placeholders,
        )


class RoomSubentryFlow(RoomWizard, ConfigSubentryFlow):
    """Add or edit a room."""

    def __init__(self) -> None:
        self._room = {}

    def _wizard_options(self) -> Mapping[str, Any]:
        return self._get_entry().options

    def _other_rooms(self) -> list[Mapping[str, Any]]:
        own = self._room.get(CONF_ROOM_ID)
        return [r for r in _rooms_of(self._get_entry()) if r.get(CONF_ROOM_ID) != own]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a room."""
        self._start_wizard(None)
        return await self.async_step_room()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a room."""
        self._start_wizard(self._get_reconfigure_subentry().data)
        return await self.async_step_room()

    async def _async_finish_room(self, room: dict[str, Any]) -> SubentryFlowResult:
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=_room_title(room),
                data=room,
            )
        return self.async_create_entry(
            title=_room_title(room), data=room, unique_id=room[CONF_ROOM_ID]
        )
