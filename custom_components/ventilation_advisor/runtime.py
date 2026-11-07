"""Home Assistant runtime: listeners, timers, storage, and delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import messages
from .const import (
    ACTIONABLE,
    CLEAR_NOTIFICATION,
    CONF_ADVISOR_ENABLED,
    CONF_CONTACTS,
    CONF_INDOOR_HUMIDITY,
    CONF_INDOOR_TEMPERATURE,
    CONF_NAME,
    CONF_OVERRIDES,
    CONF_ROOM_ID,
    CONF_THERMOSTAT,
    DOMAIN,
    PERSISTENT_TARGET,
    RECONCILE_INTERVAL_S,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .delivery import (
    Action,
    ActionType,
    Context,
    Message,
    TargetState,
    decide,
    fingerprint,
    record_failure,
    record_success,
    reminder_interval_s,
)
from .engine import Evaluation, Inputs, RoomState, evaluate
from .physics import is_quiet, last_quiet_end, next_boundary
from .readings import (
    read_contact,
    read_humidity,
    read_temperature,
    read_thermostat,
)
from .settings import merge

_LOGGER = logging.getLogger(__name__)
SAVE_DELAY_S = 10


def notification_id(room_id: str) -> str:
    """Return the stable recommendation notification ID / tag of a room."""
    return f"{DOMAIN}_{room_id}"


def problem_notification_id(room_id: str) -> str:
    """Return the stable problem notification ID of a room."""
    return f"{DOMAIN}_{room_id}_problem"


@dataclass(slots=True)
class RoomConfig:
    """Resolved room configuration."""

    room_id: str
    subentry_id: str
    name: str
    contacts: list[str]
    indoor_temperature: str | None
    indoor_humidity: str | None
    thermostat: str | None
    advisor_enabled: bool
    settings: dict[str, Any]
    sources: dict[str, str]

    @property
    def outdoor_temperature(self) -> str | None:
        """Effective outdoor temperature entity."""
        return self.settings.get("outdoor_temperature_entity")

    @property
    def outdoor_humidity(self) -> str | None:
        """Effective outdoor humidity entity."""
        return self.settings.get("outdoor_humidity_entity")

    @property
    def watched(self) -> list[str]:
        """All entities whose state changes trigger evaluation."""
        ids = [
            *self.contacts,
            self.indoor_temperature,
            self.indoor_humidity,
            self.thermostat,
            self.outdoor_temperature,
            self.outdoor_humidity,
        ]
        return sorted({i for i in ids if i})

    @classmethod
    def from_subentry(
        cls, options: Mapping[str, Any], subentry: ConfigSubentry
    ) -> RoomConfig:
        """Build from the entry options and a room subentry."""
        data = subentry.data
        effective, sources = merge(options, data.get(CONF_OVERRIDES) or {})
        return cls(
            room_id=data[CONF_ROOM_ID],
            subentry_id=subentry.subentry_id,
            name=data[CONF_NAME],
            contacts=list(dict.fromkeys(data[CONF_CONTACTS])),
            indoor_temperature=data.get(CONF_INDOOR_TEMPERATURE),
            indoor_humidity=data.get(CONF_INDOOR_HUMIDITY),
            thermostat=data.get(CONF_THERMOSTAT),
            advisor_enabled=data.get(CONF_ADVISOR_ENABLED, True),
            settings=effective,
            sources=sources,
        )


@dataclass(slots=True)
class StoredRoom:
    """Stored per-room data loaded at setup."""

    state: RoomState | None = None
    delivery: dict[str, TargetState] = field(default_factory=dict)
    contacts: list[str] = field(default_factory=list)
    saved_at: datetime | None = None
    problem_displayed: bool = False
    issues: list[str] = field(default_factory=list)


class RoomRuntime:
    """Runtime of a single room."""

    def __init__(
        self,
        hass: HomeAssistant,
        advisor: Advisor,
        config: RoomConfig,
        stored: StoredRoom,
    ) -> None:
        self.hass = hass
        self.advisor = advisor
        self.config = config
        self.state: RoomState
        self.delivery: dict[str, TargetState] = stored.delivery
        self.evaluation: Evaluation | None = None
        self.next_wakeup: datetime | None = None
        self.faults: dict[str, str] = {}
        self._issues: set[str] = set(stored.issues)
        self._problem_displayed = stored.problem_displayed
        self._problem_content: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        self._pending_delivery = False
        self._stopped = False
        self._started = False
        self._init_state(stored)

    # -- state ---------------------------------------------------------------

    def _init_state(self, stored: StoredRoom) -> None:
        now = dt_util.utcnow()
        if stored.state is None:
            self.state = RoomState(history_baseline_at=now)
            return
        self.state = stored.state
        if not self._can_resume(stored):
            self.state.finalize_after_restart()

    def _can_resume(self, stored: StoredRoom) -> bool:
        """Whether a saved session provably continued (in-process reload)."""
        if self.hass.state is not CoreState.running or stored.saved_at is None:
            return False
        if sorted(stored.contacts) != sorted(self.config.contacts):
            return False
        for entity_id in self.config.contacts:
            state = self.hass.states.get(entity_id)
            if state is None or state.last_changed > stored.saved_at:
                return False
        return True

    def to_store(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {
            "state": self.state.to_dict(),
            "delivery": {k: v.to_dict() for k, v in self.delivery.items()},
            "contacts": list(self.config.contacts),
            "saved_at": dt_util.utcnow().isoformat(),
            "problem_displayed": self._problem_displayed,
            "issues": sorted(self._issues),
        }

    # -- lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Activate once Home Assistant has finished starting."""
        self._unsubs.append(async_at_started(self.hass, self._async_activate))

    @callback
    def _async_activate(self, _hass: HomeAssistant) -> None:
        if self._stopped or self._started:
            return
        self._started = True
        if watched := self.config.watched:
            self._unsubs.append(
                async_track_state_change_event(self.hass, watched, self._on_state)
            )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._on_timer,
                timedelta(seconds=RECONCILE_INTERVAL_S),
            )
        )
        self.async_evaluate()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe everything."""
        self._stopped = True
        self._pending_delivery = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer is not None:
            self._timer()
            self._timer = None
        self.next_wakeup = None

    # -- evaluation ----------------------------------------------------------

    def _inputs(self) -> Inputs:
        cfg = self.config
        contacts: dict[str, bool | None] = {}
        changed: datetime | None = None
        for entity_id in cfg.contacts:
            contacts[entity_id] = read_contact(self.hass, entity_id)
            if (state := self.hass.states.get(entity_id)) is not None:
                if changed is None or state.last_changed > changed:
                    changed = state.last_changed
        return Inputs(
            contacts=contacts,
            contacts_changed_at=changed,
            indoor_temperature=read_temperature(self.hass, cfg.indoor_temperature),
            indoor_humidity=read_humidity(self.hass, cfg.indoor_humidity),
            outdoor_temperature=read_temperature(self.hass, cfg.outdoor_temperature),
            outdoor_humidity=read_humidity(self.hass, cfg.outdoor_humidity),
            thermostat=read_thermostat(self.hass, cfg.thermostat),
            advisor_enabled=cfg.advisor_enabled,
        )

    @callback
    def _on_state(self, event: Event) -> None:
        self.async_evaluate()

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        self.async_evaluate()

    @callback
    def async_evaluate(self) -> None:
        """Evaluate synchronously, publish, and schedule delivery."""
        if self._stopped or not self._started:
            return
        now = dt_util.utcnow()
        try:
            self.evaluation = evaluate(
                self.state, self._inputs(), self.config.settings, now
            )
            self.faults.pop("evaluation", None)
        except Exception as err:
            _LOGGER.exception("Evaluation of room %s failed", self.config.name)
            self.faults["evaluation"] = repr(err)
            self.evaluation = None
        self.advisor.async_schedule_save()
        self._update_issues()
        self._schedule_delivery()

    @callback
    def _schedule(self, *candidates: datetime | None) -> None:
        if self._stopped:
            return
        now = dt_util.utcnow()
        future = [c for c in candidates if c is not None and c > now]
        wakeup = min(future) if future else None
        if wakeup == self.next_wakeup and self._timer is not None:
            return
        if self._timer is not None:
            self._timer()
            self._timer = None
        self.next_wakeup = wakeup
        if wakeup is not None:
            self._timer = async_track_point_in_utc_time(
                self.hass, self._on_timer, wakeup
            )

    # -- problems ------------------------------------------------------------

    def problem_keys(self) -> dict[str, dict[str, Any]]:
        """Return matured problems and faults keyed by a stable issue id."""
        result: dict[str, dict[str, Any]] = {}
        if self.evaluation is not None:
            for p in self.evaluation.problems:
                key = f"{self.config.room_id}_{p.role}_{p.entity_id}"
                result[key] = {
                    "role": p.role,
                    "entity_id": p.entity_id,
                    "error": p.error,
                    "since": p.since.isoformat(),
                }
        for name, error in self.faults.items():
            result[f"{self.config.room_id}_{name}"] = {
                "role": name,
                "entity_id": "",
                "error": error,
                "since": None,
            }
        return result

    @callback
    def _update_issues(self) -> None:
        if self.evaluation is None:
            return
        current = {
            f"{self.config.room_id}_{p.role}_{p.entity_id}": p
            for p in self.evaluation.problems
        }
        for issue_id in self._issues - set(current):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, p in current.items():
            if issue_id not in self._issues:
                _LOGGER.warning(
                    "Room %s: %s %s is invalid (%s)",
                    self.config.name,
                    p.role,
                    p.entity_id,
                    p.error,
                )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="input_invalid",
                translation_placeholders={
                    "room": self.config.name,
                    "role": p.role,
                    "entity_id": p.entity_id,
                    "error": p.error,
                },
            )
        self._issues = set(current)

    async def _async_update_problem_notification(self) -> None:
        settings = self.config.settings
        problems = self.evaluation.problems if self.evaluation else []
        nid = problem_notification_id(self.config.room_id)
        if not problems or not settings["persistent_notifications_enabled"]:
            if self._problem_displayed:
                persistent_notification.async_dismiss(self.hass, nid)
                self._problem_displayed = False
                self._problem_content = None
            return
        title, body = messages.render_problems(
            problems,
            room=self.config.name,
            language=settings["notification_language"],
            to_local=dt_util.as_local,
            now=dt_util.utcnow(),
        )
        content = f"{title}\n{body}"
        if content != self._problem_content:
            persistent_notification.async_create(self.hass, body, title, nid)
            self._problem_displayed = True
            self._problem_content = content

    # -- delivery ------------------------------------------------------------

    def targets(self) -> list[str]:
        """Return currently configured delivery targets."""
        settings = self.config.settings
        result: list[str] = []
        if settings["persistent_notifications_enabled"]:
            result.append(PERSISTENT_TARGET)
        result.extend(dict.fromkeys(settings["notify_actions"]))
        return result

    def desired_message(self, *, quiet: bool = False) -> Message | None:
        """Return the message that should currently be displayed."""
        ev = self.evaluation
        settings = self.config.settings
        if (
            ev is None
            or not settings["notifications_enabled"]
            or ev.recommendation not in ACTIONABLE
        ):
            return None
        permitted = [
            reason
            for reason in ev.reasons
            if settings[
                f"{reason.reason}_"
                + (
                    "goal_reached_notify_during_quiet_hours"
                    if reason.status == "resolved"
                    else "notify_during_quiet_hours"
                )
            ]
        ]
        quiet_exempt = bool(permitted) and (quiet or len(permitted) == len(ev.reasons))
        if quiet and permitted:
            ev = replace(ev, reasons=permitted)
        language = settings["notification_language"]
        title, body = messages.render(
            ev, room=self.config.name, language=language, to_local=dt_util.as_local
        )
        return Message(
            kind=str(ev.recommendation),
            session_id=ev.session_id,
            fingerprint=fingerprint(ev),
            title=title,
            body=body,
            presentation=f"{self.config.name}|{language}",
            reminder_interval_s=reminder_interval_s(ev, settings),
            quiet_exempt=quiet_exempt,
        )

    def _context(self, now: datetime) -> tuple[Context, datetime | None]:
        settings = self.config.settings
        ctx = Context(
            now=now, update_on_reason_change=settings["notify_update_on_reason_change"]
        )
        boundary: datetime | None = None
        if settings["quiet_hours_enabled"]:
            start, end = settings["quiet_hours_start"], settings["quiet_hours_end"]
            tz = dt_util.get_default_time_zone()
            ctx.quiet = is_quiet(dt_util.as_local(now), start, end)
            ctx.last_quiet_end = last_quiet_end(now, start, end, tz)
            boundary = next_boundary(now, start, end, tz)
            if ctx.quiet:
                ctx.quiet_end_at = boundary
        return ctx, boundary

    @callback
    def _schedule_delivery(self) -> None:
        if self._stopped:
            return
        self._pending_delivery = True
        if self._lock.locked():
            return
        self.advisor.entry.async_create_background_task(
            self.hass, self._async_deliver(), f"{DOMAIN} delivery {self.config.room_id}"
        )

    async def _async_deliver(self) -> None:
        async with self._lock:
            while self._pending_delivery and not self._stopped:
                self._pending_delivery = False
                deadline = await self._async_deliver_once()
                ev_deadline = self.evaluation.next_deadline if self.evaluation else None
                self._schedule(ev_deadline, deadline)

    async def _async_deliver_once(self) -> datetime | None:
        now = dt_util.utcnow()
        await self._async_update_problem_notification()
        ctx, boundary = self._context(now)
        desired = self.desired_message(quiet=ctx.quiet)
        configured = self.targets()
        deadlines: list[datetime] = []
        for target in dict.fromkeys([*configured, *self.delivery]):
            if self._stopped:
                break
            state = self.delivery.setdefault(target, TargetState())
            wanted = desired if target in configured else None
            decision = decide(
                state, wanted, ctx, persistent=target == PERSISTENT_TARGET
            )
            if decision.deadline is not None:
                deadlines.append(decision.deadline)
            if decision.action is not None:
                retry = await self._async_execute(target, state, decision.action, now)
                if retry is not None:
                    deadlines.append(retry)
                # Re-decide with the updated state to obtain follow-up
                # deadlines such as the next reminder.
                if not self._stopped:
                    self._pending_delivery = True
            if target not in configured and not state.displayed:
                self.delivery.pop(target, None)
        self._update_delivery_faults()
        if not self._stopped:
            self.advisor.async_schedule_save()
        if desired is not None and boundary is not None:
            deadlines.append(boundary)
        future = [d for d in deadlines if d > now]
        return min(future) if future else None

    async def _async_execute(
        self, target: str, state: TargetState, action: Action, now: datetime
    ) -> datetime | None:
        nid = notification_id(self.config.room_id)
        message = action.message
        try:
            if target == PERSISTENT_TARGET:
                if message is None:
                    persistent_notification.async_dismiss(self.hass, nid)
                else:
                    title = message.title
                    if action.type is ActionType.REMINDER:
                        title = messages.reminder_title(
                            title, self.config.settings["notification_language"]
                        )
                    persistent_notification.async_create(
                        self.hass, message.body, title, nid
                    )
            else:
                domain, service = target.split(".", 1)
                if message is None:
                    data: dict[str, Any] = {
                        "message": CLEAR_NOTIFICATION,
                        "data": {"tag": nid},
                    }
                else:
                    title = message.title
                    if action.type is ActionType.REMINDER:
                        title = messages.reminder_title(
                            title, self.config.settings["notification_language"]
                        )
                    data = {
                        "title": title,
                        "message": message.body,
                        "data": {"tag": nid},
                    }
                await self.hass.services.async_call(
                    domain, service, data, blocking=True
                )
        except Exception as err:  # noqa: BLE001 - any delivery failure is reported
            _LOGGER.warning(
                "Room %s: delivery to %s failed: %s", self.config.name, target, err
            )
            record_failure(state, action, now, repr(err))
            return state.retry_at
        record_success(state, action, now)
        return None

    def _update_delivery_faults(self) -> None:
        for key in [k for k in self.faults if k.startswith("delivery:")]:
            del self.faults[key]
        for target, state in self.delivery.items():
            if state.last_error is not None:
                self.faults[f"delivery:{target}"] = state.last_error


class Advisor:
    """Entry-level runtime owning all rooms and the store."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self.rooms: dict[str, RoomRuntime] = {}
        self._stored: dict[str, Any] = {}
        self._corrupt: dict[str, Any] = {}
        self.storage_error: str | None = None

    async def async_setup(self) -> None:
        """Load storage, create rooms, and start them."""
        try:
            data = await self.store.async_load() or {}
        except Exception as err:  # noqa: BLE001 - reported, not fatal
            _LOGGER.error("Ventilation Advisor storage could not be read: %s", err)
            self.storage_error = repr(err)
            data = {}
        self._stored = dict(data.get("rooms") or {})
        self._corrupt = dict(data.get("corrupt") or {})

        configured: set[str] = set()
        for subentry in self.entry.subentries.values():
            config = RoomConfig.from_subentry(self.entry.options, subentry)
            configured.add(config.room_id)
            stored = self._load_room(config.room_id)
            room = RoomRuntime(self.hass, self, config, stored)
            if self.storage_error:
                room.faults["storage"] = self.storage_error
            self.rooms[config.room_id] = room

        for room_id in set(self._stored) - configured:
            await self._async_cleanup_removed(room_id)

        for room in self.rooms.values():
            room.async_start()

    def _load_room(self, room_id: str) -> StoredRoom:
        raw = self._stored.get(room_id)
        if not raw:
            return StoredRoom()
        try:
            saved = raw.get("saved_at")
            return StoredRoom(
                state=RoomState.from_dict(raw["state"]) if raw.get("state") else None,
                delivery={
                    k: TargetState.from_dict(v)
                    for k, v in (raw.get("delivery") or {}).items()
                },
                contacts=list(raw.get("contacts") or []),
                saved_at=datetime.fromisoformat(saved) if saved else None,
                problem_displayed=bool(raw.get("problem_displayed")),
                issues=list(raw.get("issues") or []),
            )
        except (KeyError, ValueError, TypeError, AttributeError) as err:
            _LOGGER.error("Stored state of room %s is corrupt: %s", room_id, err)
            self.storage_error = f"corrupt room state: {err!r}"
            # Keep the unreadable data for diagnosis instead of overwriting it.
            self._corrupt[room_id] = raw
            self._stored.pop(room_id, None)
            return StoredRoom()

    async def _async_cleanup_removed(self, room_id: str) -> None:
        """Clear notifications/issues of a room that no longer exists."""
        raw = self._stored.pop(room_id, {}) or {}
        nid = notification_id(room_id)
        for target, data in (raw.get("delivery") or {}).items():
            if not data.get("displayed"):
                continue
            try:
                if target == PERSISTENT_TARGET:
                    persistent_notification.async_dismiss(self.hass, nid)
                else:
                    domain, service = target.split(".", 1)
                    await self.hass.services.async_call(
                        domain,
                        service,
                        {"message": CLEAR_NOTIFICATION, "data": {"tag": nid}},
                        blocking=True,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not clear %s for removed room: %s", target, err)
        persistent_notification.async_dismiss(
            self.hass, problem_notification_id(room_id)
        )
        for issue_id in raw.get("issues") or []:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _data(self) -> dict[str, Any]:
        rooms = dict(self._stored)
        rooms.update({rid: room.to_store() for rid, room in self.rooms.items()})
        data: dict[str, Any] = {"rooms": rooms}
        if self._corrupt:
            data["corrupt"] = self._corrupt
        return data

    @callback
    def async_schedule_save(self) -> None:
        """Coalesce a storage write."""
        self.store.async_delay_save(self._data, SAVE_DELAY_S)

    async def async_unload(self) -> None:
        """Stop rooms and flush state."""
        for room in self.rooms.values():
            room.async_stop()
        for room in self.rooms.values():
            async with room._lock:
                pass
            room.async_stop()
        await self.store.async_save(self._data())

    async def async_remove_all(self) -> None:
        """Clear everything owned by a removed entry and delete its storage."""
        data = await self.store.async_load() or {}
        self._stored = dict(data.get("rooms") or {})
        for room_id in list(self._stored):
            await self._async_cleanup_removed(room_id)
        await self.store.async_remove()
