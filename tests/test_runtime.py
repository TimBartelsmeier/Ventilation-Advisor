"""End-to-end tests of the integration runtime with a real HA core."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.ventilation_advisor.const import DOMAIN
from custom_components.ventilation_advisor.runtime import notification_id

from .conftest import (
    Clock,
    NotifyRecorder,
    global_options,
    room_data,
    set_state,
    settle,
    setup_entry,
)

WINDOW = "binary_sensor.bedroom_window"
TAG = notification_id("room1")
LOG_NAMESPACE = f"custom_components.{DOMAIN}"


def humid_room(hass: HomeAssistant) -> None:
    set_state(hass, WINDOW, "off")
    set_state(hass, "sensor.bedroom_temperature", "21.5", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.bedroom_humidity", "75", {"unit_of_measurement": "%"})
    set_state(hass, "sensor.outdoor_temperature", "5", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.outdoor_humidity", "80", {"unit_of_measurement": "%"})


def room_device(hass: HomeAssistant, entry_id: str):
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "room1"), entry_id
    )


def room_runtime(hass: HomeAssistant, room_id: str = "room1"):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return entry.runtime_data.rooms[room_id]


def recommendation(hass: HomeAssistant) -> str:
    return room_runtime(hass).evaluation.recommendation


def persistent(hass: HomeAssistant) -> dict | None:
    from homeassistant.components.persistent_notification import (
        _async_get_or_create_notifications,
    )

    return _async_get_or_create_notifications(hass).get(TAG)


async def test_debug_logs_trace_window_safeguard_and_delivery(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOG_NAMESPACE)
    humid_room(hass)
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            window_open_debounce=10,
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
        rooms=[room_data(indoor_humidity_entity=None)],
    )
    assert "activating; watched=" in caplog.text
    caplog.clear()
    set_state(hass, WINDOW, "on")
    await settle(hass)
    assert f"entity event {WINDOW}: off -> on" in caplog.text
    assert "window raw=open confirmed=closed" in caplog.text
    assert "open_debounce_s=10" in caplog.text
    assert "scheduling wakeup=" in caplog.text
    assert "no message; recommendation=none is not actionable" in caplog.text

    await clock.advance(11)
    assert "timer wakeup" in caplog.text
    assert "window raw=open confirmed=open" in caplog.text
    assert "maximum airing time not exceeded" in caplog.text
    await clock.advance(90)
    assert "Safeguard heat_loss" in caplog.text
    assert "adverse_effect=False quantity=temperature" in caplog.text
    assert "bound=19" in caplog.text
    assert notify.for_service("phone") == []

    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    await settle(hass)
    assert "adverse_effect=True quantity=temperature" in caplog.text
    assert "scenario=heat_loss status=active" in caplog.text
    assert "target=notify.phone" in caplog.text
    assert "decision=new_message action=send" in caplog.text
    assert "action=send succeeded" in caplog.text
    calls = notify.for_service("phone")
    assert len(calls) == 1
    assert calls[0]["message"] not in caplog.text
    assert calls[0]["title"] not in caplog.text
    set_state(hass, WINDOW, "off")
    await clock.advance(31)
    assert "window raw=closed confirmed=closed" in caplog.text
    assert "action=clear succeeded" in caplog.text


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"notifications_enabled": False}, "no message; notifications disabled"),
        (
            {"persistent_notifications_enabled": False},
            "no notification targets configured",
        ),
        (
            {
                "quiet_hours_enabled": True,
                "quiet_hours_start": "00:00",
                "quiet_hours_end": "23:59",
            },
            "decision=quiet_hours action=None",
        ),
    ],
)
async def test_debug_logs_explain_delivery_suppression(
    hass: HomeAssistant,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    options: dict,
    expected: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOG_NAMESPACE)
    humid_room(hass)
    await setup_entry(hass, options=global_options(**options))
    await clock.advance(timedelta(minutes=5, seconds=1))
    assert expected in caplog.text
    assert persistent(hass) is None


async def test_debug_logs_invalid_reading_and_recovery(
    hass: HomeAssistant,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOG_NAMESPACE)
    humid_room(hass)
    await setup_entry(hass)
    set_state(hass, "sensor.bedroom_humidity", "unavailable")
    await settle(hass)
    assert "scenario=drying_possible status=unknown" in caplog.text
    assert "'pending_problems': [" in caplog.text
    await clock.advance(timedelta(minutes=10, seconds=1))
    set_state(hass, "sensor.bedroom_humidity", "50", {"unit_of_measurement": "%"})
    await settle(hass)
    assert "input recovered; issue=" in caplog.text


async def test_debug_logs_retry_and_success(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOG_NAMESPACE)
    humid_room(hass)
    notify.fail = True
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            persistent_notifications_enabled=False,
        ),
    )
    await clock.advance(timedelta(minutes=5, seconds=1))
    assert "delivery failed; failures=1 retry_at=" in caplog.text
    assert "decision=retry_backoff action=None" in caplog.text
    notify.fail = False
    await clock.advance(31)
    assert "target=notify.phone action=send succeeded" in caplog.text
    assert len(notify.for_service("phone")) == 1


async def test_debug_snapshots_are_not_built_when_disabled(
    hass: HomeAssistant,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch,
) -> None:
    caplog.set_level(logging.INFO, logger=LOG_NAMESPACE)

    def unexpected_snapshot(*args, **kwargs):
        pytest.fail("Debug snapshot built while debug logging is disabled")

    monkeypatch.setattr(
        "custom_components.ventilation_advisor.runtime.asdict",
        unexpected_snapshot,
    )
    humid_room(hass)
    await setup_entry(hass)
    await clock.advance(301)
    assert recommendation(hass) == "open"
    assert not [
        record
        for record in caplog.records
        if record.name.startswith(LOG_NAMESPACE) and record.levelno == logging.DEBUG
    ]


async def test_no_entities_or_devices(hass: HomeAssistant, clock: Clock) -> None:
    from homeassistant.helpers import entity_registry as er

    humid_room(hass)
    entry = await setup_entry(hass)
    assert recommendation(hass) == "none"
    assert room_runtime(hass).evaluation.window == "closed"
    assert not room_runtime(hass).problem_keys()
    assert room_device(hass, entry.entry_id) is None
    assert not [
        e
        for e in er.async_get(hass).entities.values()
        if e.config_entry_id == entry.entry_id
    ]


async def test_weather_fallback_for_unconfigured_outdoor_sensors(
    hass: HomeAssistant, clock: Clock
) -> None:
    humid_room(hass)
    set_state(
        hass,
        "weather.home",
        "sunny",
        {"temperature": 12, "temperature_unit": "°C", "humidity": 65},
    )
    options = global_options(
        outdoor_temperature_sensor_entity=None,
        outdoor_humidity_sensor_entity=None,
        outdoor_weather_entity="weather.home",
    )
    await setup_entry(hass, options=options)
    runtime = room_runtime(hass)
    inputs = runtime._inputs()
    assert runtime.config.outdoor_temperature == "weather.home"
    assert runtime.config.outdoor_humidity == "weather.home"
    assert inputs.outdoor_temperature.value == 12
    assert inputs.outdoor_humidity.value == 65


async def test_configured_outdoor_sensors_override_weather_entity(
    hass: HomeAssistant, clock: Clock
) -> None:
    humid_room(hass)
    set_state(
        hass,
        "weather.home",
        "sunny",
        {"temperature": 12, "temperature_unit": "°C", "humidity": 65},
    )
    await setup_entry(
        hass,
        options=global_options(outdoor_weather_entity="weather.home"),
    )
    inputs = room_runtime(hass)._inputs()
    assert inputs.outdoor_temperature.value == 5
    assert inputs.outdoor_humidity.value == 80


async def test_open_advice_is_delivered_and_cleared(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    assert recommendation(hass) == "none"

    await clock.advance(timedelta(minutes=5, seconds=1))
    await settle(hass)
    assert recommendation(hass) == "open"
    calls = notify.for_service("phone")
    assert len(calls) == 1
    assert calls[0]["data"] == {"tag": TAG}
    assert "Bedroom" in calls[0]["title"]
    assert persistent(hass) is not None

    # Opening the window (after the debounce) satisfies the advice.
    set_state(hass, WINDOW, "on")
    await settle(hass)
    assert recommendation(hass) == "open"
    await clock.advance(61)
    await settle(hass)
    assert room_runtime(hass).evaluation.window == "open"
    assert recommendation(hass) == "none"
    assert persistent(hass) is None

    # Humidity dropped: close advice.
    set_state(hass, "sensor.bedroom_humidity", "50", {"unit_of_measurement": "%"})
    await settle(hass)
    assert recommendation(hass) == "none"
    await clock.advance(61)
    await settle(hass)
    assert recommendation(hass) == "close"

    # Closing clears everywhere.
    set_state(hass, WINDOW, "off")
    await clock.advance(31)
    await settle(hass)
    assert recommendation(hass) == "none"
    assert persistent(hass) is None
    last = notify.for_service("phone")[-1]
    assert last == {"message": "clear_notification", "data": {"tag": TAG}}
    # One notification per room: every call used the same tag.
    assert {c["data"]["tag"] for c in notify.for_service("phone")} == {TAG}


async def test_heat_loss_close_is_deferred_during_quiet_hours(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    # 23:00 Berlin time.
    clock.now = clock.now.replace(hour=22)
    set_state(hass, WINDOW, "off")
    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.bedroom_humidity", "45", {"unit_of_measurement": "%"})
    set_state(hass, "sensor.outdoor_temperature", "0", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.outdoor_humidity", "80", {"unit_of_measurement": "%"})
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            quiet_hours_enabled=True,
            heat_loss_notify_during_quiet_hours=False,
        ),
    )
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    await settle(hass)
    assert recommendation(hass) == "none"
    assert notify.for_service("phone") == []

    # The maximum expires in quiet hours: conditions become eligible, not delivered.
    await clock.advance(timedelta(minutes=20))
    await settle(hass)
    assert recommendation(hass) == "close"
    calls = notify.for_service("phone")
    assert calls == []
    assert persistent(hass) is None
    quiet_end = (clock.now + timedelta(days=1)).replace(
        hour=6, minute=0, second=1, microsecond=0
    )
    await clock.advance(quiet_end - clock.now)
    await settle(hass)
    calls = notify.for_service("phone")
    assert len(calls) == 1 and calls[0]["data"] == {"tag": TAG}
    await clock.advance(timedelta(minutes=15, seconds=1))
    await settle(hass)
    calls = notify.for_service("phone")
    assert len(calls) == 2
    assert calls[1]["title"].startswith("Reminder")


async def test_heat_loss_notifies_during_quiet_hours_by_default(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
) -> None:
    clock.now = clock.now.replace(hour=22)
    humid_room(hass)
    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            quiet_hours_enabled=True,
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
        rooms=[room_data(indoor_humidity_entity=None)],
    )
    set_state(hass, WINDOW, "on")
    await clock.advance(182)
    assert recommendation(hass) == "close"
    assert len(notify.for_service("phone")) == 1
    await clock.advance(179)
    assert len(notify.for_service("phone")) == 1
    await clock.advance(2)
    assert len(notify.for_service("phone")) == 2


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize(
    "reason",
    [
        "warming_possible",
        "cooling_possible",
        "drying_possible",
        "humidifying_possible",
        "overdue_airing",
    ],
)
async def test_quiet_permissions_are_independent_for_suggestion_and_completion(
    hass: HomeAssistant,
    clock: Clock,
    resolved: bool,
    reason: str,
) -> None:
    from custom_components.ventilation_advisor.const import Recommendation, WindowState
    from custom_components.ventilation_advisor.engine import Evaluation, ReasonInfo

    humid_room(hass)
    await setup_entry(hass)
    room = room_runtime(hass)
    status = "resolved" if resolved else "active"
    room.evaluation = Evaluation(
        evaluated_at=clock.now,
        recommendation=Recommendation.CLOSE if resolved else Recommendation.OPEN,
        window=WindowState.OPEN if resolved else WindowState.CLOSED,
        reasons=[
            ReasonInfo(
                reason,
                status,
                ("aired" if reason == "overdue_airing" else f"{reason}_target_reached")
                if resolved
                else None,
            )
        ],
        values={
            "indoor_temperature": 22.5,
            "outdoor_temperature": 18,
            "comfort_upper_temperature": 23,
            "comfort_lower_temperature": 19,
            "comfort_lower_humidity": 40,
            "comfort_upper_humidity": 55,
            "humidity_reference_temperature": 22.5,
            "indoor_reference_humidity": 50,
        },
    )
    suggestion = f"{reason}_notify_during_quiet_hours"
    completion = f"{reason}_goal_reached_notify_during_quiet_hours"
    relevant, other = (completion, suggestion) if resolved else (suggestion, completion)
    room.config.settings[other] = True
    assert not room.desired_message(quiet=True).quiet_exempt
    room.config.settings[relevant] = True
    assert room.desired_message(quiet=True).quiet_exempt


async def test_quiet_mixed_message_filters_reasons_and_reminders(
    hass: HomeAssistant,
    clock: Clock,
) -> None:
    from custom_components.ventilation_advisor.const import Recommendation, WindowState
    from custom_components.ventilation_advisor.engine import Evaluation, ReasonInfo

    humid_room(hass)
    await setup_entry(hass)
    room = room_runtime(hass)
    room.evaluation = Evaluation(
        evaluated_at=clock.now,
        recommendation=Recommendation.CLOSE,
        window=WindowState.OPEN,
        maximum_duration_s=600,
        open_for_s=660,
        reasons=[
            ReasonInfo("heat_loss", "active"),
            ReasonInfo("drying_possible", "resolved", "drying_possible_target_reached"),
        ],
        values={
            "indoor_temperature": 19,
            "outdoor_temperature": 5,
            "comfort_lower_temperature": 19,
            "humidity_reference_temperature": 21,
        },
    )
    room.config.settings["heat_loss_reminder_factor"] = 3
    room.config.settings["drying_possible_goal_reached_reminder_interval"] = 5
    quiet_message = room.desired_message(quiet=True)
    assert quiet_message.quiet_exempt
    assert quiet_message.reminder_interval_s == 1800
    assert "drying_possible" not in quiet_message.fingerprint
    assert "Drying target" not in quiet_message.body
    assert len(room.evaluation.reasons) == 2
    day_message = room.desired_message()
    assert "Drying target" in day_message.body
    assert day_message.reminder_interval_s == 300
    assert quiet_message.fingerprint != day_message.fingerprint


async def test_quiet_hours_defer_open_advice(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    # 06:00 UTC = 07:00 Berlin; start one hour earlier inside quiet hours.
    clock.now = clock.now.replace(hour=5)
    humid_room(hass)
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"], quiet_hours_enabled=True
        ),
    )
    await clock.advance(timedelta(minutes=10))
    await settle(hass)
    assert recommendation(hass) == "open"
    assert notify.for_service("phone") == []
    assert persistent(hass) is None

    await clock.advance(timedelta(minutes=51))
    await settle(hass)
    assert len(notify.for_service("phone")) == 1
    assert persistent(hass) is not None


async def test_delivery_failure_retries(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    notify.fail = True
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"], persistent_notifications_enabled=False
        ),
    )
    await clock.advance(timedelta(minutes=5, seconds=1))
    await settle(hass)
    assert recommendation(hass) == "open"
    assert room_runtime(hass).problem_keys()

    notify.fail = False
    await clock.advance(31)
    await settle(hass)
    assert len(notify.for_service("phone")) == 1
    assert not room_runtime(hass).problem_keys()


async def test_invalid_input_creates_repair_issue(
    hass: HomeAssistant, clock: Clock
) -> None:
    humid_room(hass)
    await setup_entry(hass)
    set_state(hass, "sensor.bedroom_humidity", "unavailable")
    await settle(hass)
    assert not room_runtime(hass).problem_keys()

    await clock.advance(timedelta(minutes=10, seconds=1))
    await settle(hass)
    assert room_runtime(hass).problem_keys()
    issues = [
        i for (domain, _), i in ir.async_get(hass).issues.items() if domain == DOMAIN
    ]
    assert len(issues) == 1
    assert issues[0].translation_key == "input_invalid"

    set_state(hass, "sensor.bedroom_humidity", "50", {"unit_of_measurement": "%"})
    await settle(hass)
    assert not room_runtime(hass).problem_keys()
    assert not [d for (d, _) in ir.async_get(hass).issues if d == DOMAIN]


async def test_reload_resumes_open_session(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    entry = await setup_entry(hass)
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    await settle(hass)
    session = room_runtime(hass).evaluation.session_id
    assert session is not None

    await clock.advance(60)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert room_runtime(hass).evaluation.session_id == session
    assert room_runtime(hass).evaluation.window == "open"


async def test_unverifiable_reload_finalizes_only_long_enough_airing(
    hass: HomeAssistant, clock: Clock, monkeypatch
) -> None:
    from custom_components.ventilation_advisor.runtime import RoomRuntime

    humid_room(hass)
    entry = await setup_entry(hass)
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    original = room_runtime(hass).state.session.id
    monkeypatch.setattr(RoomRuntime, "_can_resume", lambda self, stored: False)

    # Downtime cannot retroactively qualify a short interrupted opening.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert room_runtime(hass).state.last_airing_at is None
    await clock.advance(61)
    assert room_runtime(hass).state.session.id != original

    # A second interruption after sufficient observed airing does count.
    await clock.advance(timedelta(minutes=10))
    room_runtime(hass).async_evaluate()
    await settle(hass)
    end = room_runtime(hass).state.session.last_reliable_open_at
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert room_runtime(hass).state.last_airing_at == end


async def test_room_removal_cleans_up(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    entry = await setup_entry(
        hass, options=global_options(notify_actions=["notify.phone"])
    )
    await clock.advance(timedelta(minutes=5, seconds=1))
    await settle(hass)
    assert persistent(hass) is not None

    subentry_id = next(iter(entry.subentries))
    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await settle(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert persistent(hass) is None
    assert notify.for_service("phone")[-1]["message"] == "clear_notification"
    assert room_device(hass, entry.entry_id) is None


async def test_add_room_via_subentry(hass: HomeAssistant, clock: Clock) -> None:
    humid_room(hass)
    set_state(hass, "binary_sensor.office_window", "off")
    entry = await setup_entry(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=room_data(
                name="Office",
                room_id="room2",
                contacts=["binary_sensor.office_window"],
            ),
            subentry_type="room",
            title="Office",
            unique_id="room2",
        ),
    )
    await settle(hass)
    assert room_runtime(hass, "room2").evaluation is not None


async def test_unload_and_remove_entry(hass: HomeAssistant, clock: Clock) -> None:
    humid_room(hass)
    entry = await setup_entry(hass)
    await clock.advance(timedelta(minutes=5, seconds=1))
    await settle(hass)
    assert persistent(hass) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await settle(hass)
    assert persistent(hass) is None


async def test_open_reminder_is_scheduled_at_new_interval(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    await clock.advance(301)
    assert len(notify.for_service("phone")) == 1
    await clock.advance(timedelta(minutes=119))
    assert len(notify.for_service("phone")) == 1
    await clock.advance(61)
    assert len(notify.for_service("phone")) == 2
    assert notify.for_service("phone")[-1]["title"].startswith("Reminder: ")


async def test_diagnostics_without_entities(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    from custom_components.ventilation_advisor.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    humid_room(hass)
    entry = await setup_entry(
        hass, options=global_options(notify_actions=["notify.phone"])
    )
    await clock.advance(301)
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    report = await async_get_config_entry_diagnostics(hass, entry)
    room = report["rooms"]["room1"]
    assert room["evaluation"]["window"] == "open"
    assert room["state"]["session"] is not None
    assert room["participating_reasons"]["drying_possible"]["decides_close_advice"]
    assert "drying_possible_notification_enabled" in room["effective_settings"]
    assert "notify.phone" not in report["options"]["notify_actions"]
    assert "notify.phone" not in room["delivery"]
    assert report["options"]["notify_actions"] == ["notify_target_0"]
    assert "notify_target_0" in room["delivery"]


async def test_failed_evaluation_clears_previously_valid_advice(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder, monkeypatch
) -> None:
    from custom_components.ventilation_advisor import runtime

    humid_room(hass)
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    await clock.advance(301)
    assert persistent(hass) is not None

    def fail(*args, **kwargs):
        raise ValueError("invalid calculation")

    monkeypatch.setattr(runtime, "evaluate", fail)
    room_runtime(hass).async_evaluate()
    await settle(hass)
    assert room_runtime(hass).evaluation is None
    assert "evaluation" in room_runtime(hass).faults
    assert persistent(hass) is None
    assert notify.for_service("phone")[-1]["message"] == "clear_notification"


@pytest.mark.parametrize("reload_entry", [False, True])
async def test_in_flight_delivery_cannot_restart_stopped_runtime(
    hass: HomeAssistant, clock: Clock, reload_entry: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_notify(call):
        calls.append(call)
        entered.set()
        await release.wait()

    hass.services.async_register("notify", "slow", slow_notify)
    humid_room(hass)
    entry = await setup_entry(
        hass, options=global_options(notify_actions=["notify.slow"])
    )
    old = room_runtime(hass)
    clock.now += timedelta(minutes=5)
    old.async_evaluate()
    await asyncio.wait_for(entered.wait(), timeout=5)
    operation = (
        hass.config_entries.async_reload(entry.entry_id)
        if reload_entry
        else hass.config_entries.async_unload(entry.entry_id)
    )
    unloading = asyncio.create_task(operation)
    for _ in range(10):
        await asyncio.sleep(0)
        if old._stopped:
            break
    assert old._stopped
    release.set()
    assert await unloading
    await settle(hass)
    assert old._timer is None
    assert old.next_wakeup is None
    assert not old._pending_delivery
    before = len(calls)
    old.async_evaluate()
    old._schedule_delivery()
    old._schedule(clock.now + timedelta(minutes=1))
    assert old._timer is None
    if reload_entry:
        assert room_runtime(hass) is not old
        await hass.config_entries.async_unload(entry.entry_id)
    await clock.advance(timedelta(hours=3))
    assert len(calls) == before


async def test_activation_waits_for_home_assistant_started(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    hass.set_state(CoreState.starting)
    entry = await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            condition_delay=0,
        ),
    )
    room = entry.runtime_data.rooms["room1"]
    assert room.evaluation is None
    assert not room._started
    humid_room(hass)
    await settle(hass)
    assert not notify.calls
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await settle(hass)
    assert room._started
    assert room.evaluation.recommendation == "open"
    assert len(notify.calls) == 1


async def test_unload_cancels_pending_start_callback(
    hass: HomeAssistant, clock: Clock
) -> None:
    hass.set_state(CoreState.starting)
    entry = await setup_entry(hass)
    room = entry.runtime_data.rooms["room1"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    humid_room(hass)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await settle(hass)
    assert room.evaluation is None
    assert not room._started


async def test_contact_outage_retains_advice_reports_and_recovers(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    await clock.advance(301)
    before = len(notify.calls)
    set_state(hass, WINDOW, "unavailable")
    await settle(hass)
    assert recommendation(hass) == "open"
    assert room_runtime(hass).evaluation.window == "closed"
    assert len(notify.calls) == before
    assert not room_runtime(hass).problem_keys()
    await clock.advance(601)
    assert room_runtime(hass).problem_keys()
    assert any(d == DOMAIN for d, _ in ir.async_get(hass).issues)

    set_state(hass, WINDOW, "on")
    await settle(hass)
    assert recommendation(hass) == "open"
    assert not room_runtime(hass).problem_keys()
    await clock.advance(61)
    assert room_runtime(hass).evaluation.window == "open"
    assert persistent(hass) is None
    assert notify.for_service("phone")[-1]["message"] == "clear_notification"


async def test_contact_outage_does_not_postpone_heat_loss(
    hass: HomeAssistant, clock: Clock, notify: NotifyRecorder
) -> None:
    humid_room(hass)
    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.bedroom_humidity", "40", {"unit_of_measurement": "%"})
    set_state(hass, "sensor.outdoor_temperature", "-4", {"unit_of_measurement": "°C"})
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    started = room_runtime(hass).state.session.opened_at
    set_state(hass, WINDOW, "unavailable")
    await settle(hass)
    await clock.advance(540)
    assert recommendation(hass) == "close"
    assert room_runtime(hass).state.session.opened_at == started
    assert len(notify.for_service("phone")) == 1
    set_state(hass, WINDOW, "off")
    await settle(hass)
    assert recommendation(hass) == "close"
    await clock.advance(31)
    assert room_runtime(hass).evaluation.window == "closed"
    assert persistent(hass) is None


@pytest.mark.parametrize(
    ("reason", "indoor_t", "outdoor_t", "indoor_h", "outdoor_h"),
    [
        ("heat_loss", 19, 0, 45, 80),
        ("heat_gain", 23, 30, 45, 30),
        ("moisture_loss", 21, 0, 42, 80),
        ("moisture_gain", 21, 25, 56, 80),
    ],
)
async def test_manual_opening_protections_have_grace_and_share_tag(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
    reason: str,
    indoor_t: float,
    outdoor_t: float,
    indoor_h: float,
    outdoor_h: float,
) -> None:
    humid_room(hass)
    for entity, value, unit in (
        ("sensor.bedroom_temperature", indoor_t, "°C"),
        ("sensor.outdoor_temperature", outdoor_t, "°C"),
        ("sensor.bedroom_humidity", indoor_h, "%"),
        ("sensor.outdoor_humidity", outdoor_h, "%"),
    ):
        set_state(hass, entity, str(value), {"unit_of_measurement": unit})
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            moisture_loss_notification_enabled=True,
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
    )
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    assert recommendation(hass) == "none"
    assert not notify.for_service("phone")
    await clock.advance(121)
    room = room_runtime(hass)
    assert room.evaluation.recommendation == "close"
    assert reason in room.evaluation.reason_ids
    assert not room.faults
    calls = notify.for_service("phone")
    assert len(calls) == 1
    assert calls[0]["data"]["tag"] == TAG
    assert calls[0]["title"] == "Bedroom: close the window"
    assert "Maximum airing time" in calls[0]["message"]
    assert "?" not in calls[0]["message"]
    set_state(hass, WINDOW, "off")
    await clock.advance(31)
    assert persistent(hass) is None


async def test_temperature_outage_retains_protection_and_recovers(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
) -> None:
    humid_room(hass)
    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            minimum_safeguard_reminder_interval=15,
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
        rooms=[room_data(indoor_humidity_entity=None)],
    )
    set_state(hass, WINDOW, "on")
    await clock.advance(182)
    room = room_runtime(hass)
    assert recommendation(hass) == "close"
    maximum = room.evaluation.maximum_duration_s
    set_state(hass, "sensor.outdoor_temperature", "unavailable")
    await settle(hass)
    assert recommendation(hass) == "close"
    assert room.evaluation.maximum_duration_s == maximum
    assert len(notify.for_service("phone")) == 1
    await clock.advance(901)
    assert len(notify.for_service("phone")) == 2
    assert notify.for_service("phone")[-1]["title"].startswith("Reminder")
    assert room.problem_keys()
    set_state(hass, "sensor.outdoor_temperature", "20", {"unit_of_measurement": "°C"})
    await settle(hass)
    assert recommendation(hass) == "none"
    assert not room.problem_keys()
    assert persistent(hass) is None


async def test_temperature_outage_before_maximum_does_not_start_protection(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
) -> None:
    humid_room(hass)
    set_state(hass, "sensor.bedroom_temperature", "19", {"unit_of_measurement": "°C"})
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
        rooms=[room_data(indoor_humidity_entity=None)],
    )
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    assert recommendation(hass) == "none"
    set_state(hass, "sensor.outdoor_temperature", "unavailable")
    await settle(hass)
    await clock.advance(121)
    assert recommendation(hass) == "none"
    assert not notify.for_service("phone")
    set_state(hass, "sensor.outdoor_temperature", "5", {"unit_of_measurement": "°C"})
    await settle(hass)
    assert recommendation(hass) == "close"
    assert len(notify.for_service("phone")) == 1


@pytest.mark.parametrize(
    ("reason", "indoor_t", "outdoor_t", "indoor_h", "outdoor_h"),
    [
        ("warming_possible", 17.5, 22, 45, 50),
        ("humidifying_possible", 21, 21, 30, 55),
    ],
)
async def test_new_open_scenarios_are_rendered_and_delivered(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
    reason: str,
    indoor_t: float,
    outdoor_t: float,
    indoor_h: float,
    outdoor_h: float,
) -> None:
    humid_room(hass)
    for entity, value, unit in (
        ("sensor.bedroom_temperature", indoor_t, "°C"),
        ("sensor.outdoor_temperature", outdoor_t, "°C"),
        ("sensor.bedroom_humidity", indoor_h, "%"),
        ("sensor.outdoor_humidity", outdoor_h, "%"),
    ):
        set_state(hass, entity, str(value), {"unit_of_measurement": unit})
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    await clock.advance(301)
    room = room_runtime(hass)
    assert recommendation(hass) == "open"
    assert reason in room.evaluation.reason_ids
    assert not room.faults
    assert len(notify.for_service("phone")) == 1
    assert "?" not in notify.for_service("phone")[0]["message"]


async def test_combined_goals_wait_and_send_one_completion_message(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
) -> None:
    humid_room(hass)
    set_state(hass, "sensor.bedroom_temperature", "26", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.outdoor_temperature", "18", {"unit_of_measurement": "°C"})
    set_state(hass, "sensor.outdoor_humidity", "40", {"unit_of_measurement": "%"})
    await setup_entry(hass, options=global_options(notify_actions=["notify.phone"]))
    await clock.advance(301)
    room = room_runtime(hass)
    assert set(room.evaluation.reason_ids) == {"cooling_possible", "drying_possible"}
    assert len(notify.for_service("phone")) == 1
    set_state(hass, WINDOW, "on")
    await clock.advance(61)
    set_state(hass, "sensor.bedroom_temperature", "22.5", {"unit_of_measurement": "°C"})
    await settle(hass)
    await clock.advance(61)
    assert recommendation(hass) == "none"
    set_state(hass, "sensor.bedroom_humidity", "50", {"unit_of_measurement": "%"})
    await settle(hass)
    await clock.advance(61)
    assert recommendation(hass) == "close"
    assert not room.faults
    message = notify.for_service("phone")[-1]
    assert message["title"] == "Bedroom: you can close the window"
    assert "• Cooling target reached." in message["message"]
    assert "• Drying target reached." in message["message"]
    assert "Maximum airing time" not in message["message"]


async def test_heating_setpoint_shifts_window_and_message_reference(
    hass: HomeAssistant,
    clock: Clock,
    notify: NotifyRecorder,
) -> None:
    humid_room(hass)
    set_state(
        hass, "sensor.bedroom_temperature", "24.25", {"unit_of_measurement": "°C"}
    )
    set_state(
        hass, "climate.bedroom", "heat", {"temperature": 24, "temperature_unit": "°C"}
    )
    await setup_entry(
        hass,
        options=global_options(
            notify_actions=["notify.phone"],
            reference_airing_time=1,
            airing_time_lower_limit=1,
            airing_time_upper_limit=1,
        ),
        rooms=[
            room_data(thermostat_entity="climate.bedroom", indoor_humidity_entity=None)
        ],
    )
    await clock.advance(301)
    assert recommendation(hass) == "none"
    set_state(hass, WINDOW, "on")
    await clock.advance(182)
    room = room_runtime(hass)
    assert recommendation(hass) == "close"
    assert room.evaluation.values["comfort_lower_temperature"] == 24
    assert room.evaluation.values["comfort_upper_temperature"] == 25
    assert "below your heating setpoint" in notify.for_service("phone")[-1]["message"]
    assert not room.faults
