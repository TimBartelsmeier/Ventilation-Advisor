"""The Ventilation Advisor integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .runtime import Advisor

type VentilationAdvisorConfigEntry = ConfigEntry[Advisor]


async def async_setup_entry(
    hass: HomeAssistant, entry: VentilationAdvisorConfigEntry
) -> bool:
    """Set up the advisor from a config entry."""
    advisor = Advisor(hass, entry)
    await advisor.async_setup()
    entry.runtime_data = advisor
    # Options and room (subentry) changes reload the entry.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: VentilationAdvisorConfigEntry
) -> bool:
    """Unload a config entry and flush state."""
    await entry.runtime_data.async_unload()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear notifications/issues and storage of a removed entry."""
    await Advisor(hass, entry).async_remove_all()
