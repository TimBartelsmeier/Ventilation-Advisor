# Ventilation Advisor

A Home Assistant integration that helps you decide when to open or close windows in each room. It compares indoor and outdoor conditions with your comfort settings and window contacts, then sends configurable advice. It never operates windows or controls heating; configure it in Home Assistant's UI.

Depending on your settings, it can notify you when:

- A window has been open too long in cold weather and the room is getting too cool.
- Outdoor air is cool enough to help cool a warm room in summer.
- A room is overdue for airing, even if its temperature and humidity are comfortable.
- Outdoor air can help dry a humid room, such as after a shower.
- An airing goal is reached, or continued airing is starting to worsen room conditions.

> **Disclaimer:** This project is provided “as is”, without warranty of any kind. Use it at your own risk. Its recommendations are estimates, not guarantees; use your own judgment and do not rely on them as a substitute for appropriate ventilation or safety measures.
 
## Feature overview

- **Room-by-room monitoring:** Use window contacts, indoor temperature and (optionally) humidity sensors. Outdoor conditions can be shared or set per-room. Optionally, you can also pass a heating climate entity, in which case the setpoint temperature will be factored in (for example to determine which humidity the outside air will have once heated up). 
- **Suggestions to open:** Get advice to cool, warm, dry or humidify a room when outdoor air can help, or when the room is overdue for airing.
- **Airing progress:** Get an estimated recommended airing time and, optionally, a notification when the goals active at opening time (like dehumidifying the room) are reached.
- **Suggestions to close:** If an opening continues beyond its maximum airing time and outdoor air is worsening conditions near a comfort limit, get a reminder to close.
- **Notification controls:** Choose scenarios, reminders, quiet-hours behavior and delivery targets globally or per room.

## Installation
**Recommended: Home Assistant Marketplace** (called **HACS** before 2026.11). With Marketplace installed, open its menu → **Custom repositories**, add `https://github.com/TimBartelsmeier/Ventilation-Advisor` with type **Integration**, then find and download **Ventilation Advisor**.

**Manual alternative:** copy `custom_components/ventilation_advisor` from this repository into your Home Assistant `config/custom_components/` folder.

After either installation:

1. Restart Home Assistant.
2. Open **Settings → Devices & services → Add integration → Ventilation Advisor**.
3. Configure global settings, then add your rooms.

Global settings can be edited with **Configure**. Rooms use **Add room** and **Reconfigure**, with the same options available during setup and editing.

## Core concepts

### Openings and comfort

A room counts as open when at least one of its window contacts has remained open through the configured debounce period. The opening ends after all contacts have been confirmed closed.

Each room has a comfort range, **19–23 °C** and **40–55 % relative humidity** by default. Readings can be below, within or above that range. To avoid notifications switching rapidly at a boundary, a reading must move past a small hysteresis margin to change state: for example, temperature becomes too high above 23.5 °C and returns to range at 22.5 °C. At the lower temperature bound, it becomes too low below 18.5 °C and returns at 19.5 °C. Humidity uses the same pattern with a default 3-percentage-point margin (temperature uses 0.5 °C); margins shrink for narrow comfort ranges.

Outdoor air must make a meaningful difference to trigger comfort advice: at least **2 °C** or **5 humidity percentage points** by default. Once advice is active, that useful effect ends only after the difference falls below half its threshold.

### Optional heating climate entity

The selected climate entity is assumed to provide **heating only**; its setpoint `S` predicts the temperature the heater will restore after the window closes. In heating mode, the integration uses the target, or the lower target of a temperature range. When heating is off or has no target, the configured temperature comfort range applies.

The effective temperature bounds are:

```text
lower = max(configured lower, S)
upper = max(configured upper, lower + 1 °C)
```

A setpoint of 21 °C gives a 21–23 °C window. A setpoint of 24 °C gives 24–25 °C, avoiding advice to cool below a temperature the heater will restore. A night setback below the configured lower bound does not lower the comfort range.

Humidity advice compares indoor and outdoor air at the same reference temperature: `max(measured indoor temperature, S)`, or the measured indoor temperature when there is no heating target. Their water content is converted to equivalent relative humidity at that temperature. This accounts for cold outdoor air becoming drier as it warms indoors; outdoor relative humidity by itself cannot tell whether airing will dry the room. The configured humidity comfort range does not change.

### Recommended and maximum airing times

The recommended time `t_rec` estimates an airing long enough for an air exchange. It is a simplified guideline, not a measurement of ventilation or air quality. Actual exchange also depends on wind, window opening and room geometry.

```text
t_rec = reference_airing_time × sqrt(10 °C / abs(indoor − outdoor temperature))
        limited to airing_time_lower_limit … airing_time_upper_limit
t_max = t_rec × maximum_airing_time_factor
```

Defaults are **10 minutes at a 10 K difference**, limited to **5–45 minutes**, with a maximum factor of **1.5**. Equal or unavailable temperatures use the upper limit. Both times are fixed at opening confirmation; subsequent changes do not move the deadline. The calculation uses measured temperatures, not the heating setpoint.

| Temperature difference | Recommended | Maximum |
|---|---|---|
| 25 °C | 6.3 min | 9.5 min |
| 10 °C | 10 min | 15 min |
| 5 °C | 14.1 min | 21.2 min |
| 2 °C | 22.4 min | 33.5 min |
| 0 °C | 45 min | 67.5 min |

Increase the reference time for large rooms, small windows or tilted openings; decrease it for faster exchange. The maximum factor must be at least 1. An opening lasting at least `t_rec` counts as a qualifying airing, including manual openings. A shorter opening does not reset overdue-airing history.

## Scenarios

### Suggest opening

Opening suggestions apply while all room windows are closed. Each comfort scenario is evaluated independently, so drying can be suggested even if it temporarily cools a room that is already cold. Overdue-airing advice is independent of comfort and simply reminds you to air the room if it hasn't been for some time. A scenario must remain true for the configured condition delay before its advice starts.

| Scenario | When opening helps |
|---|---|
| Cooling possible | Temperature is above comfort and outdoor air is meaningfully colder. |
| Warming possible | Temperature is below comfort and outdoor air is meaningfully warmer. |
| Drying possible | Reference RH is above comfort and outdoor air can reduce water content. |
| Humidifying possible | Reference RH is below comfort and outdoor air can increase water content. |
| Overdue airing | No qualifying airing has occurred within the airing interval; repeat notifications use the separate reminder interval. |

### Optional “goal reached” advice

When a window opens, the integration captures the opening reasons active at that time, including reasons that started in the previous 15 minutes. These are the goals for that opening; reasons that appear later are not added.

- A comfort goal is reached when its reading returns within the comfort range (at the hysteresis threshold) or outdoor air is no longer useful for that goal.
- An overdue-airing goal is reached after `t_rec`. Reached goals stay reached for the rest of the opening.
- When **all captured goals with goal-reached notifications enabled** are reached, one notification says **“you can close the window.”** At least one such goal is required; opening a window manually does not by itself create completion advice.

This is permission to close, not a command to stop airing. A humidity goal is based on water content at the reference temperature, so measured relative humidity may differ while the room is temporarily cooled.

### Suggest closing

Close safeguards apply to every opening, but **only after `t_max`**. They warn when outdoor air is making a room near a comfort limit worse:

| Scenario | Adverse effect near or beyond the comfort edge |
|---|---|
| Heat loss | Room near/below the lower temperature bound; outdoor air below that bound cools it further. |
| Heat gain | Room near/above the upper temperature bound; outdoor air above that bound warms it further. |
| Moisture loss | Reference RH near/below the lower humidity bound; outdoor air beyond that bound dries it further. |
| Moisture gain | Reference RH near/above the upper humidity bound; outdoor air beyond that bound humidifies it further. |

“Near” means within one hysteresis margin of the comfort limit. The effect must also be meaningful. A safeguard clears when the room moves more than two margins inside the limit or outdoor air is no longer making conditions worse.

Exceeding the maximum alone does not trigger a notification. Useful cooling can continue through the comfort window toward its lower edge. With heating enabled, that edge follows the heating setpoint.

Safeguards can advise closing while goals remain unfinished, without declaring them reached. If a safeguard stops applying, obsolete advice is cleared or replaced with eligible goal-reached advice.

## Options and defaults

**Global + room** means a room can override the global value individually. Comfort settings and primary notification switches are visible normally; delays, reminders and technical tuning use collapsible advanced sections. Review shows inheritance, effective heating-adjusted bounds and airing times. Effective room settings are validated when global settings change.

### Sources

| Option | Required | Scope |
|---|---|---|
| Window contacts | One or more; each contact belongs to only one room | room |
| Indoor temperature | Yes | room |
| Indoor humidity | For humidity scenarios | room |
| Heating climate entity | No | room |
| Weather entity | Required if no outdoor temperature sensor is selected; also supplies humidity when no humidity sensor is selected | global |
| Outdoor temperature sensor | Optional; takes precedence over the weather entity's temperature | global |
| Outdoor humidity sensor | Optional; takes precedence over the weather entity's humidity; one of these sources is needed for humidity scenarios | global |

Selectors filter by corresponding entity/device classes.

### Comfort and airing

| Option key | Default | Scope |
|---|---|---|
| `comfort_lower_temperature` / `comfort_upper_temperature` | 19 / 23 °C | global + room |
| `comfort_lower_humidity` / `comfort_upper_humidity` | 40 / 55 % | global + room |
| `reference_airing_time` | 10 min | global + room |
| `maximum_airing_time_factor` | 1.5 | global + room |
| `airing_time_lower_limit` / `airing_time_upper_limit` | 5 / 45 min | global + room |

### Per-scenario notifications

Every scenario has `<scenario>_notification_enabled` and `<scenario>_reminder_interval`. The overdue-airing scenario also has `airing_interval`, which determines how long since a qualifying airing before the first suggestion is due. Its separate `overdue_airing_reminder_interval` controls how often to repeat the suggestion while airing remains overdue. Opening scenarios additionally have `<scenario>_goal_reached_notification_enabled` and `<scenario>_goal_reached_reminder_interval`. Goal-reached advice requires that scenario's main notification switch. These settings are global with room overrides.

Every suggestion type also has `<scenario>_notify_during_quiet_hours`. Opening scenarios have a separate `<scenario>_goal_reached_notify_during_quiet_hours` for completion advice. These switches apply to initial notifications and reminders. All default to **off**, except **Heat loss suggestions**, which default to **on**.

| Scenario key | Enabled | Goal reached | Reminder | Goal-reached reminder |
|---|---|---|---|---|
| `cooling_possible`, `warming_possible`, `drying_possible`, `humidifying_possible` | on | on | 120 min | 0 (off) |
| `overdue_airing` | on | on | 120 min | 0 (off) |
| `heat_loss`, `heat_gain`, `moisture_gain` | on | — | 15 min | — |
| `moisture_loss` | off | — | 15 min | — |

Goal-reached reminders default to **0 (disabled)**. Zero disables a reminder; set a positive interval to enable it. A global reminder switch can disable all reminders. When advice contains several reasons, the shortest positive applicable interval is used.

The overdue-airing defaults are an **8-hour airing interval** and a **120-minute reminder interval**. The first determines when the room becomes due for airing; the second controls repeat notifications after overdue advice has started.

### General and advanced

| Option | Default | Scope |
|---|---|---|
| Send notifications / persistent notifications | on / on | global + room |
| Notify actions | none | global |
| Update notify messages when reasons change | on | global + room |
| Notification language | English (default) or German | global |
| Quiet hours enabled, start, end | on, 22:00–07:00 | global + room |
| Reminders enabled | on | global + room |
| `condition_delay` / `goal_reached_delay` | 5 / 1 min | global |
| `minimum_temperature_difference` | 2 °C | global |
| `minimum_humidity_difference` | 5 percentage points at reference temperature | global |
| Window open / close debounce | 60 / 30 s | global |
| Problem report delay | 10 min | global |

## Notification delivery and unavailable inputs

Advice titles identify the room and suggested action without repeating them in the body. Each message shows indoor and outdoor conditions once, followed by a short introduction and reason bullets. Humidity advice includes equivalent RH in parentheses when comparison temperatures differ: outdoors at indoor temperature, or both readings at the heating setpoint when reheating is expected. Opening advice includes the recommended full-air-exchange time; safeguards show elapsed and maximum airing times. Evaluation timestamps are not included.

- **One notification per room**, replaced using the same tag. Close advice can combine reached goals and active safeguards. Advice is cleared when no longer applicable or the opening ends.
- **Targets:** Home Assistant persistent notifications and legacy `notify.*` actions. Tagged replacement/clearing requires Companion App devices or groups; email/Telegram-style services may accumulate messages and show clear commands as text. Modern notify entities using `notify.send_message` are not supported.
- **Quiet hours** hold back types without “Notify during quiet hours” enabled. Mixed room notifications include only permitted reasons and use their reminder intervals. Goal-reached permission is controlled separately from its opening suggestion. Problem/Repairs reporting is unaffected. At quiet-hours end, current held-back advice is sent immediately, combined per room; conditions that disappeared are not replayed. Unchanged advice already delivered is not immediately resent: non-exempt reminders count their interval from quiet-hours end. Types allowed during quiet hours keep their normal cadence.
- Changed advice uses the same tag; notify reason-change alerts follow their configured switch. Numerical updates alone do not restart reminder timing. Failed deliveries retry after 30 seconds, 2 minutes and 5 minutes, with failures exposed in diagnostics.
- **Unavailable/invalid readings freeze dependent scenarios:** existing state and reminders are retained; new suggestions do not start and dependent goals do not finish. Valid readings resume normal evaluation. Retained advice can be outdated during an outage.
- Contacts keep their last confirmed state and opening timer during an outage. Recovery uses normal debounce. Without a previous valid observation, window state remains unknown.
- Persistent input faults produce Repairs issues and optional persistent problem notifications after the report delay.

There are no integration entities or room devices. Integration diagnostics show effective settings, sensor faults, captured goals, frozen times and delivery state. Activation waits for Home Assistant startup. Full restart conservatively ends interrupted openings using the last reliable observation; a settings reload resumes a provably continuous opening.

## Development

```bash
uv sync
uv run pytest
uv run python -m scripts.generate_translations
```

The generator uses settings metadata to produce the English configuration strings in `strings.json` and `translations/en.json`. Tests check synchronization. Notifications are available in English and German; the language is selected globally.

## Planned improvements and ideas

- Additional configuration languages.
- Seasonal close advice using heating season, outdoor averages or forecasts: for example, protect summer coolness before reaching the upper comfort edge.
- Humidity influence on recommended airing times.
- Room snoozing and vacation mode.
- Bundled notifications and silent reminder/content updates.
- Unavailable sensors in rooms with multiple sensors, including dead batteries.
- Entities and/or cards for dashboard integration.
