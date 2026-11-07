"""Server-side rendering of English base notification strings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import Reason, Recommendation
from .engine import Evaluation, Problem, ReasonInfo
from .physics import absolute_humidity, relative_humidity

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title.open": "{room}: open the window",
        "title.close": "{room}: you can close the window",
        "title.protection": "{room}: close the window",
        "reminder": "Reminder: {title}",
        "intro.open": "Opening a window might be beneficial to:",
        "intro.close": "Window may be closed again:",
        "intro.protection": "Window should be closed:",
        "conditions.indoor": "Indoors: {conditions}",
        "conditions.outdoor": "Outdoors: {conditions}",
        "conditions.temperature": "{value} °C",
        "conditions.humidity": "{value} % RH",
        "conditions.unavailable": "unavailable",
        "conditions.equivalent": " ({value} % at {reference})",
        "conditions.reference.indoor": "indoor temperature",
        "conditions.reference.heating": "heating setpoint",
        "reason.drying_possible": "Reduce excess humidity.",
        "reason.cooling_possible": "Cool the room toward comfort.",
        "reason.warming_possible": "Warm the room toward comfort.",
        "reason.warming_possible.heating": "Warm the room toward your heating setpoint.",
        "reason.humidifying_possible": "Increase humidity toward comfort.",
        "reason.overdue_airing": "The room is overdue for fresh air.",
        "resolution.drying_possible_target_reached": "Drying target reached.",
        "resolution.drying_possible_no_longer_helpful": (
            "Outdoor air no longer helps dry the room."
        ),
        "resolution.cooling_possible_target_reached": "Cooling target reached.",
        "resolution.cooling_possible_no_longer_helpful": (
            "Outdoor air no longer helps cool the room."
        ),
        "resolution.warming_possible_target_reached": "Warming target reached.",
        "resolution.warming_possible_no_longer_helpful": (
            "Outdoor air no longer helps warm the room."
        ),
        "resolution.humidifying_possible_target_reached": "Humidifying target reached.",
        "resolution.humidifying_possible_no_longer_helpful": (
            "Outdoor air no longer helps humidify the room."
        ),
        "reason.heat_loss": ("Further airing risks cooling below your comfort range."),
        "reason.heat_loss.heating": (
            "Further airing risks cooling below your heating setpoint."
        ),
        "reason.heat_gain": ("Further airing risks warming above your comfort range."),
        "reason.moisture_loss": "Further airing risks making the room too dry.",
        "reason.moisture_gain": "Further airing risks making the room too humid.",
        "airing.preview": "Recommended for full air exchange: {recommended}",
        "resolution.aired": "Recommended fresh-air airing completed.",
        "protection.due": ("Window open: {open_for} · Maximum airing time: {maximum}"),
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
        "title.open": "{room}: Fenster öffnen",
        "title.close": "{room}: Fenster kann wieder geschlossen werden",
        "title.protection": "{room}: Fenster schließen",
        "reminder": "Erinnerung: {title}",
        "intro.open": "Fenster öffnen könnte helfen bei:",
        "intro.close": "Fenster kann wieder geschlossen werden:",
        "intro.protection": "Fenster sollte geschlossen werden:",
        "conditions.indoor": "Innen: {conditions}",
        "conditions.outdoor": "Außen: {conditions}",
        "conditions.temperature": "{value} °C",
        "conditions.humidity": "{value} % rF",
        "conditions.unavailable": "nicht verfügbar",
        "conditions.equivalent": " ({value} % bei {reference})",
        "conditions.reference.indoor": "Raumtemperatur",
        "conditions.reference.heating": "Heizungs-Solltemperatur",
        "reason.drying_possible": "Feuchtigkeit senken.",
        "reason.cooling_possible": "Raum Richtung Komfortbereich kühlen.",
        "reason.warming_possible": "Raum Richtung Komfortbereich erwärmen.",
        "reason.warming_possible.heating": "Raum Richtung Heizungs-Solltemperatur erwärmen.",
        "reason.humidifying_possible": "Feuchtigkeit erhöhen.",
        "reason.overdue_airing": "Frischluft ist fällig.",
        "resolution.drying_possible_target_reached": "Trocknungsziel erreicht.",
        "resolution.drying_possible_no_longer_helpful": (
            "Außenluft trocknet den Raum nicht mehr."
        ),
        "resolution.cooling_possible_target_reached": "Kühlziel erreicht.",
        "resolution.cooling_possible_no_longer_helpful": (
            "Außenluft kühlt den Raum nicht mehr."
        ),
        "resolution.warming_possible_target_reached": "Erwärmungsziel erreicht.",
        "resolution.warming_possible_no_longer_helpful": (
            "Außenluft erwärmt den Raum nicht mehr."
        ),
        "resolution.humidifying_possible_target_reached": "Befeuchtungsziel erreicht.",
        "resolution.humidifying_possible_no_longer_helpful": (
            "Außenluft befeuchtet den Raum nicht mehr."
        ),
        "reason.heat_loss": (
            "Weiteres Lüften kann den Raum unter den Komfortbereich abkühlen."
        ),
        "reason.heat_loss.heating": (
            "Weiteres Lüften kann unter die Heizungs-Solltemperatur abkühlen."
        ),
        "reason.heat_gain": (
            "Weiteres Lüften kann den Raum über den Komfortbereich erwärmen."
        ),
        "reason.moisture_loss": ("Weiteres Lüften kann die Luft zu trocken machen."),
        "reason.moisture_gain": ("Weiteres Lüften kann die Luft zu feucht machen."),
        "airing.preview": (
            "Empfohlene Dauer für vollständigen Luftaustausch: {recommended}"
        ),
        "resolution.aired": "Empfohlene Frischluftdauer erreicht.",
        "protection.due": (
            "Fenster offen: {open_for} · Maximale Lüftungsdauer: {maximum}"
        ),
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
    text = f"{value:.1f}"
    return text.replace(".", ",") if language == "de" else text


def _duration(seconds: float | None, language: str) -> str:
    if seconds is None:
        return "?"
    total = max(0.0, round(seconds / 60, 1))
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


def _reason_line(
    info: ReasonInfo,
    evaluation: Evaluation,
    language: str,
) -> str:
    values = evaluation.values | (info.sample or {})
    if info.status == "resolved" and info.code:
        return _t(language, f"resolution.{info.code}")
    suffix = (
        ".heating"
        if info.reason in (Reason.WARMING_POSSIBLE, Reason.HEAT_LOSS)
        and _uses_heating_bound(values)
        else ""
    )
    return _t(language, f"reason.{info.reason}{suffix}")


def _conditions(evaluation: Evaluation, language: str) -> list[str]:
    values: dict[str, float] = {}
    for info in evaluation.reasons:
        if info.status != "resolved":
            values.update(info.sample)
    values.update(evaluation.values)
    indoor = values.get("indoor_temperature")
    setpoint = values.get("heating_setpoint")
    reference = (
        max(indoor, setpoint) if indoor is not None and setpoint is not None else indoor
    )
    reheating = indoor is not None and reference is not None and reference > indoor
    humidity_reason = any(
        info.reason
        in (
            Reason.DRYING_POSSIBLE,
            Reason.HUMIDIFYING_POSSIBLE,
            Reason.MOISTURE_LOSS,
            Reason.MOISTURE_GAIN,
        )
        for info in evaluation.reasons
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
            text = _t(language, "conditions.humidity", value=_num(humidity, language))
            if (
                humidity_reason
                and reference is not None
                and temperature is not None
                and temperature != reference
            ):
                equivalent = relative_humidity(
                    reference, absolute_humidity(temperature, humidity)
                )
                text += _t(
                    language,
                    "conditions.equivalent",
                    value=_num(equivalent, language),
                    reference=_t(
                        language,
                        "conditions.reference.heating"
                        if reheating
                        else "conditions.reference.indoor",
                    ),
                )
            parts.append(text)
        lines.append(
            _t(
                language,
                f"conditions.{side}",
                conditions=", ".join(parts)
                if parts
                else _t(language, "conditions.unavailable"),
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
    rec = evaluation.recommendation
    assert rec is not None
    protection = rec is Recommendation.CLOSE and any(
        info.reason in ("heat_loss", "heat_gain", "moisture_loss", "moisture_gain")
        and info.status in ("active", "releasing")
        for info in evaluation.reasons
    )
    title = _t(
        language, "title.protection" if protection else f"title.{rec}", room=room
    )
    lines = _conditions(evaluation, language)
    lines.extend(
        ("", _t(language, "intro.protection" if protection else f"intro.{rec}"))
    )
    for info in evaluation.reasons:
        lines.append(f"• {_reason_line(info, evaluation, language)}")
    if protection:
        lines.extend(
            (
                "",
                _t(
                    language,
                    "protection.due",
                    open_for=_duration(evaluation.open_for_s, language),
                    maximum=_duration(evaluation.maximum_duration_s, language),
                ),
            )
        )
    if rec is Recommendation.OPEN and evaluation.preview_duration_s is not None:
        lines.extend(
            (
                "",
                _t(
                    language,
                    "airing.preview",
                    recommended=_duration(evaluation.preview_duration_s, language),
                ),
            )
        )
    return title, "\n".join(lines)


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
