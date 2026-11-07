"""Diagnostics for the Ventilation Advisor."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import VentilationAdvisorConfigEntry
from .const import PERSISTENT_TARGET


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VentilationAdvisorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    advisor = entry.runtime_data
    targets = dict.fromkeys(entry.options.get("notify_actions", []))
    for room in advisor.rooms.values():
        targets.update(dict.fromkeys(room.delivery))
    target_labels = {
        target: f"notify_target_{i}"
        for i, target in enumerate(t for t in targets if t != PERSISTENT_TARGET)
    }
    target_labels[PERSISTENT_TARGET] = PERSISTENT_TARGET
    options = dict(entry.options)
    if "notify_actions" in options:
        options["notify_actions"] = [
            target_labels[target] for target in options["notify_actions"]
        ]
    rooms: dict[str, Any] = {}
    for room_id, room in advisor.rooms.items():
        cfg = room.config
        settings = dict(cfg.settings)
        settings["notify_actions"] = len(settings["notify_actions"])
        ev = room.evaluation
        session = room.state.session
        rooms[room_id] = {
            "name": cfg.name,
            "contacts": cfg.contacts,
            "indoor_temperature": cfg.indoor_temperature,
            "indoor_humidity": cfg.indoor_humidity,
            "thermostat": cfg.thermostat,
            "advisor_enabled": cfg.advisor_enabled,
            "effective_settings": settings,
            "setting_sources": cfg.sources,
            "evaluation": asdict(ev) if ev else None,
            "state": room.state.to_dict(),
            "participating_reasons": {
                str(reason): {
                    **asdict(participant),
                    "decides_close_advice": cfg.settings[
                        f"{reason}_goal_reached_notification_enabled"
                    ],
                }
                for reason, participant in (session.reasons.items() if session else ())
            },
            "delivery": {
                target_labels[target]: {
                    "displayed": st.displayed,
                    "fingerprint": st.fingerprint,
                    "last_semantic_at": st.last_semantic_at,
                    "failures": st.failures,
                    "retry_at": st.retry_at,
                    "last_error": st.last_error,
                }
                for target, st in room.delivery.items()
            },
            "faults": room.faults,
            "next_wakeup": room.next_wakeup,
        }
    return {
        "options": options,
        "storage_error": advisor.storage_error,
        "rooms": rooms,
    }
