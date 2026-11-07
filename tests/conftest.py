"""Shared fixtures for Home Assistant level tests.

``pytest-homeassistant-custom-component`` does not support the installed Home
Assistant version, so a minimal ``hass`` fixture with a controllable clock is
provided here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import bootstrap, core, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import event as ha_event
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.ventilation_advisor import settings
from custom_components.ventilation_advisor.const import DOMAIN, SUBENTRY_ROOM

REPO_COMPONENTS = Path(__file__).resolve().parent.parent / "custom_components"
START = datetime(2026, 1, 15, 10, 0, tzinfo=dt_util.UTC)


class Clock:
    """Frozen, manually advanced clock driving HA's time trackers."""

    def __init__(self, hass: HomeAssistant, now: datetime) -> None:
        self.hass = hass
        self.now = now

    def utcnow(self) -> datetime:
        return self.now

    def timestamp(self) -> float:
        return self.now.timestamp()

    def _due_trackers(self, until: datetime) -> list[tuple[float, Any, Any]]:
        handles = []
        for handle in self.hass.loop._scheduled:
            track = getattr(handle, "_callback", None)
            if handle.cancelled() or not isinstance(
                track,
                ha_event._TrackPointUTCTime,
            ):
                continue
            if track.expected_fire_timestamp <= until.timestamp():
                handles.append((track.expected_fire_timestamp, handle, track))
        return sorted(handles, key=lambda item: item[0])

    async def advance(self, delta: timedelta | float) -> None:
        """Move time forward, firing due point-in-time trackers in order."""
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        target = self.now + delta
        await self.hass.async_block_till_done()
        while due := self._due_trackers(target):
            fire_at, handle, track = due[0]
            self.now = max(self.now, dt_util.utc_from_timestamp(fire_at))
            handle.cancel()
            track()
            await self.hass.async_block_till_done()
        self.now = target
        await self.hass.async_block_till_done()


@dataclass
class NotifyRecorder:
    """Records calls to mock ``notify.*`` services."""

    calls: list[ServiceCall] = field(default_factory=list)
    fail: bool = False

    def for_service(self, service: str) -> list[dict[str, Any]]:
        return [dict(c.data) for c in self.calls if c.service == service]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "custom_components").mkdir()
    os.symlink(REPO_COMPONENTS / DOMAIN, tmp_path / "custom_components" / DOMAIN)
    return tmp_path


@pytest.fixture
def clock_patch() -> Iterator[dict[str, Clock]]:
    holder: dict[str, Clock] = {}

    def utcnow() -> datetime:
        return holder["clock"].now if "clock" in holder else datetime.now(dt_util.UTC)

    def timestamp() -> float:
        return utcnow().timestamp()

    with (
        patch.object(dt_util, "utcnow", utcnow),
        patch.object(ha_event, "time_tracker_utcnow", utcnow),
        patch.object(ha_event, "time_tracker_timestamp", timestamp),
    ):
        yield holder


@pytest.fixture
async def hass(
    config_dir: Path, clock_patch: dict[str, Clock]
) -> AsyncIterator[HomeAssistant]:
    hass = core.HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.config.skip_pip = True
    await bootstrap.async_load_base_functionality(hass)
    await hass.config_entries.async_initialize()
    clock_patch["clock"] = Clock(hass, START)
    hass.data["test_clock"] = clock_patch["clock"]
    await hass.async_start()
    await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()
    yield hass
    await hass.async_stop(force=True)


@pytest.fixture
def clock(hass: HomeAssistant) -> Clock:
    return hass.data["test_clock"]


@pytest.fixture
def notify(hass: HomeAssistant) -> NotifyRecorder:
    recorder = NotifyRecorder()

    async def handle(call: ServiceCall) -> None:
        if recorder.fail:
            raise RuntimeError("delivery failed")
        recorder.calls.append(call)

    for name in ("phone", "tablet"):
        hass.services.async_register("notify", name, handle)
    return recorder


def set_state(
    hass: HomeAssistant,
    entity_id: str,
    state: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Set a state stamped with the frozen clock."""
    hass.states.async_set(
        entity_id, state, attributes, timestamp=dt_util.utcnow().timestamp()
    )


def room_data(
    name: str = "Bedroom",
    room_id: str = "room1",
    contacts: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return room subentry data."""
    data: dict[str, Any] = {
        "room_id": room_id,
        "name": name,
        "advisor_enabled": True,
        "contacts": contacts or ["binary_sensor.bedroom_window"],
        "indoor_temperature_entity": "sensor.bedroom_temperature",
        "indoor_humidity_entity": "sensor.bedroom_humidity",
        "thermostat_entity": None,
        "overrides": {},
    }
    data.update(extra)
    return data


def global_options(**changes: Any) -> dict[str, Any]:
    """Return complete global options with test-friendly defaults."""
    options = settings.global_settings({})
    options.update(
        outdoor_temperature_entity="sensor.outdoor_temperature",
        outdoor_humidity_entity="sensor.outdoor_humidity",
        quiet_hours_enabled=False,
    )
    options.update(changes)
    return options


async def setup_entry(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    rooms: list[dict[str, Any]] | None = None,
) -> ConfigEntry:
    """Add and set up a config entry."""
    if rooms is None:
        rooms = [room_data()]
    entry = ConfigEntry(
        data={},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options=options if options is not None else global_options(),
        source="user",
        subentries_data=[
            {
                "data": room,
                "subentry_type": SUBENTRY_ROOM,
                "title": room["name"],
                "unique_id": room["room_id"],
            }
            for room in rooms
        ],
        title="Ventilation Advisor",
        unique_id=None,
        version=1,
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    return entry


async def settle(hass: HomeAssistant) -> None:
    """Let background delivery tasks finish."""
    for _ in range(3):
        await hass.async_block_till_done()
        await asyncio.sleep(0)
