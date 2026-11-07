"""Generate translations/en.json from the settings definition.

Run: uv run --no-sync python -m scripts.generate_translations
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.ventilation_advisor.settings import (
    LANGUAGES,
    QUIET_GROUP,
    SCENARIO_DESCRIPTIONS,
    SECTION_LABELS,
    SECTIONS,
    SETTINGS,
    Kind,
    Scope,
)

TARGET = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ventilation_advisor"
    / "translations"
    / "en.json"
)

OUTDOOR_KEYS = ("outdoor_temperature_entity", "outdoor_humidity_entity")
MODE = "_mode"

ERRORS = {
    "invalid_settings": "Some settings are invalid:\n{details}",
    "invalid_name": "Enter a name with 1–80 characters.",
    "duplicate_name": "Another room already uses this name.",
    "contacts_required": "Select at least one window or door contact.",
    "contact_in_use": "A selected contact is already assigned to another room.",
    "entity_required": "Select the required entity.",
    "invalid_entity": "Select an entity of the required type.",
}

ROOM_FIELDS = {
    "name": ("Name", "Used in notification titles."),
    "advisor_enabled": (
        "Advisor enabled",
        "Turn off to pause advice for this room; airing history is still tracked.",
    ),
    "contacts": (
        "Window and door contacts",
        (
            "Binary sensors where on means open. The room counts as open when any "
            "contact is open."
        ),
    ),
    "indoor_temperature_entity": (
        "Indoor temperature",
        "Required. Temperature sensor used for temperature advice and airing times.",
    ),
    "indoor_humidity_entity": (
        "Indoor relative humidity",
        "Optional. Needed for drying, humidifying, moisture-loss and moisture-gain advice.",
    ),
    "thermostat_entity": (
        "Climate (heating-only)",
        (
            "Optional, heating-only. An active heating setpoint lifts the lower "
            "temperature bound to max(comfort lower, setpoint), and the upper bound "
            "to max(comfort upper, effective lower + 1 °C). For a range target, "
            "the lower end is used. Indoor and outdoor humidity are judged at "
            "max(indoor temperature, setpoint); the humidity window stays unchanged."
        ),
    ),
}


def _global_step(title: str) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    for section_id in SECTIONS:
        data: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        for s in SETTINGS:
            if s.section != section_id:
                continue
            data[s.key] = s.label
            if s.description:
                descriptions[s.key] = s.description
        if data:
            sections[section_id] = {
                "name": SECTION_LABELS[section_id],
                "data": data,
                "data_description": descriptions,
            }
            if section_id == "airing_time_advanced":
                sections[section_id]["description"] = (
                    "Recommended time = reference time × √(10 K / temperature "
                    "difference), clamped to these limits. Maximum time = "
                    "recommended time × maximum factor. At differences of "
                    "25/10/5/2/0 K, defaults give approximately 5.1/8/11.3/17.9/30 "
                    "minutes recommended and 7.6/12/17/26.8/45 minutes maximum. "
                    "Both are fixed at confirmed opening."
                )
            elif section_id == "comfort":
                sections[section_id]["description"] = (
                    "Independent temperature and humidity comfort goals. Fixed "
                    "hysteresis is 0.5 °C / 3 %RH, capped at one quarter of each "
                    "window's width. An active heating setpoint adjusts the "
                    "effective temperature window and humidity reference."
                )
            elif section_id in SCENARIO_DESCRIPTIONS:
                sections[section_id]["description"] = SCENARIO_DESCRIPTIONS[section_id]
    return {
        "title": title,
        "description": (
            "Shared settings for all rooms. Rooms can override most of them."
        ),
        "sections": sections,
    }


def _overrides_step() -> dict[str, Any]:
    sections: dict[str, Any] = {}
    for section_id in SECTIONS:
        data: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        quiet_done = False
        for s in SETTINGS:
            if (
                s.section != section_id
                or s.scope is not Scope.SHARED
                or s.key in OUTDOOR_KEYS
            ):
                continue
            if s.kind is Kind.BOOL:
                data[s.key] = s.label
                if s.description:
                    descriptions[s.key] = s.description
            elif s.group == QUIET_GROUP:
                if not quiet_done:
                    data[f"{QUIET_GROUP}{MODE}"] = "Quiet hours times"
                    descriptions[f"{QUIET_GROUP}{MODE}"] = (
                        "Start and end are overridden together."
                    )
                    quiet_done = True
                data[s.key] = s.label
            else:
                data[f"{s.key}{MODE}"] = s.label
                data[s.key] = f"{s.label} (room value)"
                if s.description:
                    descriptions[f"{s.key}{MODE}"] = s.description
        if data:
            sections[section_id] = {
                "name": SECTION_LABELS[section_id],
                "data": data,
                "data_description": descriptions,
            }
            if section_id in SCENARIO_DESCRIPTIONS:
                sections[section_id]["description"] = SCENARIO_DESCRIPTIONS[section_id]
    return {
        "title": "Room settings",
        "description": (
            "Each option uses the shared value unless you choose a room value. "
            "Airing times are frozen during each opening."
        ),
        "sections": sections,
    }


def _room_steps() -> dict[str, Any]:
    return {
        "room": {
            "title": "Room",
            "data": {k: v[0] for k, v in ROOM_FIELDS.items()},
            "data_description": {k: v[1] for k, v in ROOM_FIELDS.items() if v[1]},
        },
        "overrides": _overrides_step(),
        "review": {"title": "Review", "description": "{summary}"},
    }


def build() -> dict[str, Any]:
    """Build the English translation resource."""
    errors = dict(ERRORS)
    return {
        "config": {
            "step": {
                "user": _global_step("Ventilation Advisor"),
                "first_room": {
                    "title": "Rooms",
                    "description": "Add your first room now or later.",
                    "menu_options": {
                        "add_room": "Add a room",
                        "finish": "Finish without rooms",
                    },
                },
                **_room_steps(),
            },
            "error": errors,
            "abort": {"single_instance_allowed": "Already configured."},
        },
        "options": {
            "step": {"init": _global_step("Shared settings")},
            "error": errors,
        },
        "config_subentries": {
            "room": {
                "entry_type": "Room",
                "initiate_flow": {"user": "Add room", "reconfigure": "Edit room"},
                "step": _room_steps(),
                "error": errors,
                "abort": {"reconfigure_successful": "Room updated."},
            }
        },
        "selector": {
            "override_mode": {
                "options": {
                    "shared": "Use shared",
                    "custom": "Room value",
                    "on": "On",
                    "off": "Off",
                }
            },
            "language": {
                "options": {
                    language: label
                    for language, label in (("en", "English"), ("de", "German"))
                    if language in LANGUAGES
                }
            },
        },
        "issues": {
            "input_invalid": {
                "title": "{room}: {entity_id} is invalid",
                "description": (
                    "The {role} input {entity_id} of room {room} reports {error}. "
                    "Scenarios that need it are frozen until it recovers: active "
                    "reasons and reminders are retained, new reasons do not start, "
                    "and dependent goals are not reached."
                ),
            }
        },
    }


def render() -> str:
    """Return the JSON text for en.json."""
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """Write the translation file."""
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render(), encoding="utf-8")
    TARGET.parent.parent.joinpath("strings.json").write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
