"""Translation file consistency."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.ventilation_advisor import messages, settings
from scripts import generate_translations

EN = Path("custom_components/ventilation_advisor/translations/en.json")
STRINGS = Path("custom_components/ventilation_advisor/strings.json")


def test_en_json_is_up_to_date() -> None:
    assert EN.read_text(encoding="utf-8") == generate_translations.render()
    assert STRINGS.read_text(encoding="utf-8") == generate_translations.render()
    data = json.loads(EN.read_text(encoding="utf-8"))
    assert "entity" not in data
    assert "area_id" not in data["config"]["step"]["room"]["data"]
    assert "location_phrase" not in EN.read_text(encoding="utf-8")
    assert "airing_time_advanced" in data["options"]["step"]["init"]["sections"]
    assert "comfort" in data["options"]["step"]["init"]["sections"]
    room = data["config"]["step"]["room"]
    assert "Required" in room["data_description"]["indoor_temperature_entity"]
    assert "heating-only" in room["data"]["thermostat_entity"]
    assert not set(generate_translations.OUTDOOR_KEYS).intersection(room["data"])
    assert not any("outdoor_" in key for key in room["data"])
    assert data["selector"]["language"]["options"] == {
        "en": "English",
        "de": "German",
    }


def test_comfort_and_independent_goal_wording() -> None:
    data = generate_translations.build()
    sections = data["config"]["step"]["user"]["sections"]
    assert "one quarter" in sections["comfort"]["description"]
    assert (
        "max(lower comfort limit, setpoint)"
        in sections["comfort"]["data_description"]["comfort_lower_temperature"]
    )
    assert (
        "max(upper comfort limit, effective lower bound + 1 °C)"
        in sections["comfort"]["data_description"]["comfort_upper_temperature"]
    )
    assert (
        "max(indoor temperature, heating setpoint)"
        in sections["comfort"]["data_description"]["comfort_lower_humidity"]
    )
    assert (
        "independent"
        in sections["drying_possible"]["data_description"][
            "drying_possible_goal_reached_notification_enabled"
        ]
    )
    assert (
        "after the maximum airing time"
        in sections["heat_loss"]["data_description"]["heat_loss_notification_enabled"]
    )
    assert "√" in sections["airing_time_advanced"]["description"]
    overrides = data["config"]["step"]["overrides"]["sections"]
    assert "outdoor" not in overrides and "advanced" not in overrides
    for scenario in (*settings.SCENARIOS, *settings.PROTECT_SCENARIOS):
        assert sections[scenario]["description"]
        assert overrides[scenario]["description"] == sections[scenario]["description"]
    overdue_fields = sections["overdue_airing"]
    assert "airing_interval" in overdue_fields["data"]
    assert "overdue_airing_reminder_interval" in overdue_fields["data"]
    assert (
        "repeat notifications"
        in overdue_fields["data_description"]["overdue_airing_reminder_interval"]
    )
    overdue_overrides = overrides["overdue_airing"]
    assert "airing_interval" in overdue_overrides["data"]
    assert "overdue_airing_reminder_interval" in overdue_overrides["data"]


def test_notify_wording_explains_legacy_action_limit() -> None:
    data = json.loads(EN.read_text(encoding="utf-8"))
    for step in (data["config"]["step"]["user"], data["options"]["step"]["init"]):
        section = step["sections"]["notifications"]
        assert section["data"]["notify_actions"] == "Legacy notify actions"
        description = section["data_description"]["notify_actions"]
        assert "Modern notify entities" in description
        assert "notify.send_message" in description
        assert "not supported" in description


def test_every_setting_has_label_and_description() -> None:
    data = json.loads(EN.read_text(encoding="utf-8"))
    step = data["config"]["step"]["user"]
    labelled: set[str] = set()
    for section in step["sections"].values():
        labelled |= set(section["data"])
    for key in settings.SETTINGS_BY_KEY:
        if key.startswith(settings.QUIET_GROUP) or key in labelled:
            continue
        raise AssertionError(f"missing label for {key}")


def _strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def test_placeholders_are_well_formed() -> None:
    data = json.loads(EN.read_text(encoding="utf-8"))
    for text in _strings(data):
        stripped = re.sub(r"\{[a-z_]+\}", "", text)
        assert "{" not in stripped and "}" not in stripped, text


def test_german_notification_strings_cover_english_base() -> None:
    assert messages.STRINGS["de"].keys() == messages.STRINGS["en"].keys()
    assert messages.ROLE_LABELS["de"].keys() == messages.ROLE_LABELS["en"].keys()
    for key, english in messages.STRINGS["en"].items():
        german_placeholders = set(
            re.findall(r"\{([a-z_]+)\}", messages.STRINGS["de"][key])
        )
        english_placeholders = set(re.findall(r"\{([a-z_]+)\}", english))
        assert german_placeholders == english_placeholders, key
