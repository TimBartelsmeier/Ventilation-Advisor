"""Server-side rendering of English and German notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import Reason, Recommendation
from .engine import Evaluation, Problem, ReasonInfo
from .physics import absolute_humidity, relative_humidity

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "reminder": "Reminder: {title}",
        "conditions.temperature": "{value} °C",
        "conditions.unavailable": "unavailable",
        "minutes": "{n} min",
        "hours": "{h} h {m} min",
        "problem.title": "{room}: Ventilation Advisor problem",
        "problem.line": "{role} ({entity_id}): {error} since {since}.",
        "problem.footer": (
            "Affected scenarios keep their last state until the input recovers; "
            "no new advice starts from unavailable readings."
        ),
    },
    "de": {
        "reminder": "Erinnerung: {title}",
        "conditions.temperature": "{value} °C",
        "conditions.unavailable": "nicht verfügbar",
        "minutes": "{n} Min.",
        "hours": "{h} Std. {m} Min.",
        "problem.title": "{room}: Problem mit Ventilation Advisor",
        "problem.line": "{role} ({entity_id}): {error} seit {since}.",
        "problem.footer": (
            "Betroffene Szenarien behalten ihren letzten Zustand. "
            "Bei nicht verfügbaren Eingaben startet keine neue Empfehlung."
        ),
    },
}

ADVICE: dict[str, dict[str, str]] = {
    "en": {
        "and": " and ",
        "condition": "{room} is {conditions}",
        "cold": "cold",
        "hot": "hot",
        "humid": "humid",
        "dry": "dry",
        "getting.cold": "getting cold",
        "getting.hot": "getting hot",
        "getting.humid": "getting humid",
        "getting.dry": "getting dry",
        "drying_possible": "dry",
        "cooling_possible": "cool",
        "warming_possible": "warm",
        "humidifying_possible": "humidify",
        "goal.drying_possible": "drying",
        "goal.cooling_possible": "cooling",
        "goal.warming_possible": "warming",
        "goal.humidifying_possible": "humidifying",
        "quantity.drying_possible": "Humidity",
        "quantity.cooling_possible": "Temperature",
        "quantity.warming_possible": "Temperature",
        "quantity.humidifying_possible": "Humidity",
        "open": "Outdoor air can {actions} the room; open a window.",
        "overdue.title": "{room} is due for airing",
        "overdue": "It is time for fresh air; open a window.",
        "overdue.also": "Fresh air is also due.",
        "preview": "Full air exchange: about {duration}.",
        "timing": "Open for {duration} · Airing limit about {maximum}.",
        "goal.title.one": "{room} {goals} target reached",
        "goal.title.many": "{room} {goals} targets reached",
        "goal.body": "{quantities} {verb} back in range; you can close the window.",
        "is": "is",
        "are": "are",
        "goal.warming.heating": "The room has warmed above your heating setpoint; you can close the window.",
        "goal.also.one": "The {goals} target has also been reached.",
        "goal.also.many": "The {goals} targets have also been reached.",
        "unhelpful.title": "{room}: outdoor air no longer helps {goals}",
        "unhelpful": "Outdoor air no longer {actions} the room",
        "unhelpful.drying_possible": "dries",
        "unhelpful.cooling_possible": "cools",
        "unhelpful.warming_possible": "warms",
        "unhelpful.humidifying_possible": "humidifies",
        "close.optional": "; you can close the window.",
        "aired.title": "{room} airing completed",
        "aired": "The recommended fresh-air duration has been reached",
        "aired.also": "The recommended fresh-air duration has also been reached.",
        "goals.reached": "{quantities} {verb} back in range.",
        "protect.near.heat_loss": "Further airing risks cooling below your {bound}; close the window.",
        "protect.near.heat_gain": "Further airing risks warming above your comfort range; close the window.",
        "protect.near.moisture_loss": "Further airing risks making the room too dry; close the window.",
        "protect.near.moisture_gain": "Further airing risks making the room too humid; close the window.",
        "protect.beyond.heat_loss": "Outdoor air cools the room further; close the window.",
        "protect.beyond.heat_gain": "Outdoor air warms the room further; close the window.",
        "protect.beyond.moisture_loss": "Outdoor air dries the room further; close the window.",
        "protect.beyond.moisture_gain": "Outdoor air humidifies the room further; close the window.",
        "effect.heat_loss": "cooling",
        "effect.heat_gain": "warming",
        "effect.moisture_loss": "drying",
        "effect.moisture_gain": "humidifying",
        "protect.many": "Avoid further {effects}; close the window.",
        "bound.heating": "heating setpoint",
        "bound.comfort": "comfort range",
        "inside": "Inside: {conditions}",
        "outside": "Outside: {conditions}",
        "setpoint": " (heating setpoint {temperature} °C)",
        "equivalent": " ({humidity}% RH @ {temperature} °C)",
        "rh": "{humidity}% RH",
    },
    "de": {
        "and": " und ",
        "condition": "{room} ist {conditions}",
        "cold": "kalt",
        "hot": "warm",
        "humid": "feucht",
        "dry": "trocken",
        "getting.cold": "bald zu kalt",
        "getting.hot": "bald zu warm",
        "getting.humid": "bald zu feucht",
        "getting.dry": "bald zu trocken",
        "drying_possible": "trocknen",
        "cooling_possible": "kühlen",
        "warming_possible": "wärmen",
        "humidifying_possible": "befeuchten",
        "goal.drying_possible": "Trocknung",
        "goal.cooling_possible": "Kühlung",
        "goal.warming_possible": "Erwärmung",
        "goal.humidifying_possible": "Befeuchtung",
        "quantity.drying_possible": "Luftfeuchte",
        "quantity.cooling_possible": "Temperatur",
        "quantity.warming_possible": "Temperatur",
        "quantity.humidifying_possible": "Luftfeuchte",
        "open": "Außenluft kann den Raum {actions}; Fenster öffnen.",
        "overdue.title": "{room}: Lüften fällig",
        "overdue": "Zeit für Frischluft; Fenster öffnen.",
        "overdue.also": "Frischluft ist ebenfalls fällig.",
        "preview": "Vollständiger Luftaustausch: etwa {duration}",
        "timing": "Offen seit {duration} · Lüftungsgrenze etwa {maximum}",
        "goal.title.one": "{room}: Ziel erreicht ({goals})",
        "goal.title.many": "{room}: Ziele erreicht ({goals})",
        "goal.body": "{quantities} {verb} wieder im Komfortbereich; Fenster kann geschlossen werden.",
        "is": "ist",
        "are": "sind",
        "goal.warming.heating": "Der Raum ist über die Heizungs-Solltemperatur erwärmt; Fenster kann geschlossen werden.",
        "goal.also.one": "Ziel ebenfalls erreicht: {goals}.",
        "goal.also.many": "Ziele ebenfalls erreicht: {goals}.",
        "unhelpful.title": "{room}: Außenluft hilft nicht mehr ({goals})",
        "unhelpful": "Außenluft kann den Raum nicht mehr {actions}",
        "unhelpful.drying_possible": "trocknen",
        "unhelpful.cooling_possible": "kühlen",
        "unhelpful.warming_possible": "wärmen",
        "unhelpful.humidifying_possible": "befeuchten",
        "close.optional": "; Fenster kann geschlossen werden.",
        "aired.title": "{room}: Lüften abgeschlossen",
        "aired": "Die empfohlene Frischluftdauer ist erreicht",
        "aired.also": "Die empfohlene Frischluftdauer ist ebenfalls erreicht.",
        "goals.reached": "{quantities} {verb} wieder im Komfortbereich.",
        "protect.near.heat_loss": "Weiteres Lüften kann unter den {bound} abkühlen; Fenster schließen.",
        "protect.near.heat_gain": "Weiteres Lüften kann über den Komfortbereich erwärmen; Fenster schließen.",
        "protect.near.moisture_loss": "Weiteres Lüften kann die Luft zu trocken machen; Fenster schließen.",
        "protect.near.moisture_gain": "Weiteres Lüften kann die Luft zu feucht machen; Fenster schließen.",
        "protect.beyond.heat_loss": "Außenluft kühlt den Raum weiter ab; Fenster schließen.",
        "protect.beyond.heat_gain": "Außenluft erwärmt den Raum weiter; Fenster schließen.",
        "protect.beyond.moisture_loss": "Außenluft trocknet den Raum weiter; Fenster schließen.",
        "protect.beyond.moisture_gain": "Außenluft befeuchtet den Raum weiter; Fenster schließen.",
        "effect.heat_loss": "Abkühlen",
        "effect.heat_gain": "Erwärmen",
        "effect.moisture_loss": "Austrocknen",
        "effect.moisture_gain": "Befeuchten",
        "protect.many": "Weiteres {effects} vermeiden; Fenster schließen.",
        "bound.heating": "Heizungs-Sollwert",
        "bound.comfort": "Komfortbereich",
        "inside": "Innen: {conditions}",
        "outside": "Außen: {conditions}",
        "setpoint": " (Heizungs-Solltemperatur {temperature} °C)",
        "equivalent": " ({humidity}% rF @ {temperature} °C)",
        "rh": "{humidity}% rF",
    },
}

ROLE_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "contact": "Window contact",
        "indoor_temperature": "Indoor temperature",
        "indoor_humidity": "Indoor humidity",
        "outdoor_temperature": "Outdoor temperature",
        "outdoor_humidity": "Outdoor humidity",
        "thermostat": "Thermostat",
    },
    "de": {
        "contact": "Fensterkontakt",
        "indoor_temperature": "Innentemperatur",
        "indoor_humidity": "Relative Luftfeuchtigkeit innen",
        "outdoor_temperature": "Außentemperatur",
        "outdoor_humidity": "Relative Luftfeuchtigkeit außen",
        "thermostat": "Thermostat",
    },
}


def _t(language: str, key: str, **kwargs: Any) -> str:
    table = STRINGS.get(language) or STRINGS["en"]
    template = table.get(key) or STRINGS["en"][key]
    return template.format(**kwargs)


def _num(value: float | None, language: str) -> str:
    if value is None:
        return "?"
    text = f"{value:.1f}".removesuffix(".0")
    return text.replace(".", ",") if language == "de" else text


def _duration(seconds: float | None, language: str) -> str:
    if seconds is None:
        return "?"
    total = max(0, round(seconds / 60))
    decimal = lambda value: (
        f"{value:g}".replace(".", ",") if language == "de" else f"{value:g}"
    )
    if total < 60:
        return _t(language, "minutes", n=decimal(total))
    return _t(language, "hours", h=int(total // 60), m=decimal(total % 60))


def _when(value: datetime, now: datetime, language: str) -> str:
    if value.date() == now.date():
        return value.strftime("%H:%M")
    return value.strftime("%d.%m.%Y %H:%M" if language == "de" else "%Y-%m-%d %H:%M")


def _uses_heating_bound(values: dict[str, float]) -> bool:
    lower = values.get("comfort_lower_temperature")
    setpoint = values.get("heating_setpoint")
    return setpoint is not None and lower == setpoint


def _a(language: str, key: str, **kwargs: Any) -> str:
    return ADVICE.get(language, ADVICE["en"])[key].format(**kwargs)


def _join(items: list[str], language: str) -> str:
    return _a(language, "and").join(dict.fromkeys(items))


def _values(evaluation: Evaluation) -> dict[str, float]:
    values: dict[str, float] = {}
    for info in evaluation.reasons:
        if info.status != "resolved":
            values.update(info.sample)
    values.update(evaluation.values)
    return values


def _conditions(evaluation: Evaluation, language: str) -> list[str]:
    values = _values(evaluation)
    indoor = values.get("indoor_temperature")
    setpoint = values.get("heating_setpoint")
    reference = (
        max(indoor, setpoint) if indoor is not None and setpoint is not None else indoor
    )
    thermal_reason = any(
        info.reason
        in (
            Reason.WARMING_POSSIBLE,
            Reason.COOLING_POSSIBLE,
            Reason.HEAT_LOSS,
            Reason.HEAT_GAIN,
        )
        for info in evaluation.reasons
    )
    show_setpoint = setpoint is not None and (
        (indoor is not None and setpoint > indoor)
        or (thermal_reason and _uses_heating_bound(values))
    )
    lines = []
    for side in ("indoor", "outdoor"):
        temperature = values.get(f"{side}_temperature")
        humidity = values.get(f"{side}_humidity")
        parts = []
        if temperature is not None:
            parts.append(
                _t(
                    language,
                    "conditions.temperature",
                    value=_num(temperature, language),
                )
            )
        if humidity is not None:
            text = _a(language, "rh", humidity=round(humidity))
            if side == "outdoor" and reference is not None and temperature is not None:
                equivalent = relative_humidity(
                    reference, absolute_humidity(temperature, humidity)
                )
                text += _a(
                    language,
                    "equivalent",
                    humidity=round(equivalent),
                    temperature=_num(reference, language),
                )
            parts.append(text)
        conditions = (
            " · ".join(parts) if parts else _t(language, "conditions.unavailable")
        )
        if side == "indoor" and show_setpoint:
            conditions += _a(language, "setpoint", temperature=_num(setpoint, language))
        lines.append(
            _a(
                language,
                "inside" if side == "indoor" else "outside",
                conditions=conditions,
            )
        )
    return lines


def render(
    evaluation: Evaluation,
    *,
    room: str,
    language: str,
    to_local: Any,
) -> tuple[str, str]:
    """Return (title, body) for an actionable evaluation."""
    assert evaluation.recommendation in (Recommendation.OPEN, Recommendation.CLOSE)
    if evaluation.recommendation is Recommendation.OPEN:
        title, lines = _opening(evaluation, room, language)
    else:
        title, lines = _closing(evaluation, room, language)
    return title, "\n".join([*lines, "", *_conditions(evaluation, language)])


OPEN_CONDITIONS = {
    Reason.DRYING_POSSIBLE: "humid",
    Reason.COOLING_POSSIBLE: "hot",
    Reason.WARMING_POSSIBLE: "cold",
    Reason.HUMIDIFYING_POSSIBLE: "dry",
}
PROTECT_CONDITIONS = {
    Reason.HEAT_LOSS: ("cold", "temperature", "lower", -1),
    Reason.HEAT_GAIN: ("hot", "temperature", "upper", 1),
    Reason.MOISTURE_LOSS: ("dry", "humidity", "lower", -1),
    Reason.MOISTURE_GAIN: ("humid", "humidity", "upper", 1),
}


def _opening(ev: Evaluation, room: str, language: str) -> tuple[str, list[str]]:
    reasons = list(dict.fromkeys(info.reason for info in ev.reasons))
    comfort = [reason for reason in reasons if reason in OPEN_CONDITIONS]
    if comfort:
        title = _a(
            language,
            "condition",
            room=room,
            conditions=_join(
                [_a(language, OPEN_CONDITIONS[r]) for r in comfort], language
            ),
        )
        body = _a(
            language,
            "open",
            actions=_join([_a(language, r) for r in comfort], language),
        )
        if Reason.OVERDUE_AIRING in reasons:
            body += " " + _a(language, "overdue.also")
    else:
        title, body = _a(language, "overdue.title", room=room), _a(language, "overdue")
    lines = [body]
    if ev.preview_duration_s is not None:
        lines.append(
            _a(language, "preview", duration=_duration(ev.preview_duration_s, language))
        )
    return title, lines


def _goal_text(
    infos: list[ReasonInfo],
    room: str,
    language: str,
    *,
    additional: bool,
    values: dict[str, float],
) -> tuple[str, list[str]]:
    reached = [
        i.reason
        for i in infos
        if i.reason in OPEN_CONDITIONS and i.code == f"{i.reason}_target_reached"
    ]
    unhelpful = [
        i.reason
        for i in infos
        if i.reason in OPEN_CONDITIONS and i.code == f"{i.reason}_no_longer_helpful"
    ]
    aired = any(i.code == "aired" for i in infos)
    goals = _join([_a(language, f"goal.{r}") for r in reached], language)
    quantities = list(dict.fromkeys(_a(language, f"quantity.{r}") for r in reached))
    quantity_text = _join(quantities, language)
    verb = _a(language, "is" if len(quantities) == 1 else "are")
    # English sentence-case names become lowercase after a conjunction.
    if language != "de" and len(quantities) > 1:
        quantity_text = quantity_text[0] + quantity_text[1:].lower()
    lines = []
    title = _a(language, "aired.title", room=room)
    if reached:
        count = "one" if len(reached) == 1 else "many"
        title = _a(language, f"goal.title.{count}", room=room, goals=goals)
        if additional:
            lines.append(_a(language, f"goal.also.{count}", goals=goals))
        elif unhelpful or aired:
            lines.append(
                _a(language, "goals.reached", quantities=quantity_text, verb=verb)
            )
        elif reached == [Reason.WARMING_POSSIBLE] and _uses_heating_bound(values):
            lines.append(_a(language, "goal.warming.heating"))
        else:
            lines.append(_a(language, "goal.body", quantities=quantity_text, verb=verb))
    if unhelpful:
        names = _join([_a(language, f"goal.{r}") for r in unhelpful], language)
        if not reached:
            title = _a(language, "unhelpful.title", room=room, goals=names)
        text = _a(
            language,
            "unhelpful",
            actions=_join(
                [_a(language, f"unhelpful.{r}") for r in unhelpful], language
            ),
        )
        lines.append(
            text + ("." if additional or aired else _a(language, "close.optional"))
        )
    if aired:
        lines.append(
            _a(language, "aired.also")
            if additional
            else _a(language, "aired") + _a(language, "close.optional")
        )
    return title, lines


def _closing(ev: Evaluation, room: str, language: str) -> tuple[str, list[str]]:
    values = _values(ev)
    safeguards = [
        i.reason
        for i in ev.reasons
        if i.reason in PROTECT_CONDITIONS and i.status in ("active", "releasing")
    ]
    resolved = [i for i in ev.reasons if i.status == "resolved"]
    goal_title, goal_lines = _goal_text(
        resolved, room, language, additional=bool(safeguards), values=values
    )
    if not safeguards:
        return goal_title, goal_lines
    states = []
    beyond = {}
    for reason in safeguards:
        condition, quantity, edge, direction = PROTECT_CONDITIONS[reason]
        indoor = (
            values.get("indoor_temperature")
            if quantity == "temperature"
            else values.get("indoor_reference_humidity", values.get("indoor_humidity"))
        )
        bound = values.get(f"comfort_{edge}_{quantity}")
        beyond[reason] = (
            indoor is not None
            and bound is not None
            and direction * (indoor - bound) > 0
        )
        states.append(
            _a(language, condition if beyond[reason] else f"getting.{condition}")
        )
    title = _a(language, "condition", room=room, conditions=_join(states, language))
    if len(safeguards) == 1:
        reason = safeguards[0]
        body = _a(
            language,
            f"protect.{'beyond' if beyond[reason] else 'near'}.{reason}",
            bound=_a(
                language,
                "bound.heating" if _uses_heating_bound(values) else "bound.comfort",
            ),
        )
    else:
        body = _a(
            language,
            "protect.many",
            effects=_join([_a(language, f"effect.{r}") for r in safeguards], language),
        )
    lines = [body, *goal_lines]
    lines.append(
        _a(
            language,
            "timing",
            duration=_duration(ev.open_for_s, language),
            maximum=_duration(ev.maximum_duration_s, language),
        )
    )
    return title, lines


def reminder_title(title: str, language: str) -> str:
    """Mark a title as reminder."""
    return _t(language, "reminder", title=title)


def render_problems(
    problems: list[Problem], *, room: str, language: str, to_local: Any, now: datetime
) -> tuple[str, str]:
    """Return (title, body) for the room's problem notification."""
    local_now = to_local(now)
    labels = ROLE_LABELS.get(language) or ROLE_LABELS["en"]
    lines = [
        _t(
            language,
            "problem.line",
            role=labels.get(p.role, p.role),
            entity_id=p.entity_id,
            error=p.error,
            since=_when(to_local(p.since), local_now, language),
        )
        for p in problems
    ]
    lines.append(_t(language, "problem.footer"))
    return _t(language, "problem.title", room=room), "\n".join(lines)
