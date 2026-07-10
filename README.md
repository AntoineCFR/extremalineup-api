# ExtremaLineUp API

> REST backend for **[FestCompanion](../../FestCompanion/festcompanion)** — a multi-festival companion app.
> A lightweight Flask service that sits over Google BigQuery and orchestrates push notifications, geolocation, and a live weather feed.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white">
  <img alt="Flask" src="https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white">
  <img alt="BigQuery" src="https://img.shields.io/badge/Google-BigQuery-4285F4?logo=googlecloud&logoColor=white">
  <img alt="Firebase" src="https://img.shields.io/badge/Firebase-Admin%20SDK-FFCA28?logo=firebase&logoColor=black">
  <img alt="Render" src="https://img.shields.io/badge/Deployed%20on-Render-46E3B7">
</p>

---

## Overview

This service is the data and orchestration layer behind the FestCompanion mobile app. It is intentionally a **thin, stateless API** — BigQuery is the single source of truth, and the API's job is to expose clean JSON endpoints, run the small amount of business logic that doesn't belong on the client (stage resolution, fan-out notifications, weather caching), and keep secrets server-side.

The app is **multi-festival**: a single `festivals` table holds each event's metadata (name, city, dates, timezone), and every data table carries a `festival_id`. User accounts are **global** (shared across festivals); per-festival presence (live GPS + current stage) lives in a `festival_users` join table.

```
Flutter app  ──HTTPS/JSON──►  Flask API  ──►  BigQuery (source of truth)
   (festival_id)                  │
                                  ├──►  Firebase Admin SDK  (push → all_users topic)
                                  └──►  WeatherAPI          (forecast → cached in BQ)
```

| | |
|---|---|
| **Framework** | Flask 3 + Flask-CORS, served by Gunicorn |
| **Data store** | Google BigQuery (project `extremalineup`, dataset `dataset`) |
| **Hosting** | Render |
| **Integrations** | Firebase Cloud Messaging (Admin SDK), WeatherAPI |

> **Terminology:** what used to be called a *district* is now a **stage** (a geolocated performance area with four corner coordinates + a rally point). The timetable's `host` field is the collective/brand curating a stage.

---

## Multi-festival model

| Table | Scope | Notes |
|---|---|---|
| `festivals` | — | Metadata: `festival_id`, `slug`, `name`, `city`, `country`, `start_date`, `end_date`, `timezone`, `is_active`. Replaces the constants previously hard-coded in `config.py`. |
| `users` | **global** | One account per person, reused across festivals (`id`, `username`, `phone_number`, `user_role`). |
| `festival_users` | per-festival | Live presence: `(festival_id, user_id, last_lat, last_lng, last_location, last_seen_at)`. `last_seen_at` is bumped only on a real GPS fix (`upsert_bigquery_festival_user`), not by a stage re-resolve with no new coordinates. |
| `timetable`, `user_favorites`, `stages`, `geoloc`, `events`, `weather` | per-festival | All carry a `festival_id` column. |

Every data endpoint **requires** a `festival_id` (query param on `GET`, body field on `POST`/`PUT`/`DELETE`) and returns a `400` if it is missing.

---

## API Reference

### Festivals
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/festivals[?active_only=true]` | List festivals (for the selection screen) |
| `GET` | `/api/festivals/<festival_id>` | A single festival's metadata |

### Line-up & users
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/timetable?festival_id=` | Full line-up, ordered by day/stage/time (times shifted to the festival's local TZ) |
| `GET` | `/users?festival_id=` | Users present on a festival (id, username, phone, last location & stage, last-seen timestamp, tent location, role) |
| `GET` | `/users/check?username=` | Resolve a username to its user id (global, no `festival_id`) |
| `POST` | `/users/<id>/phone` | Update a user's phone number (global) |
| `POST` | `/users/<id>/location` | Update a user's coordinates on a festival (`festival_id` in body) |
| `POST` | `/users/<id>/tent` | Set a user's tent/camp location on a festival (`festival_id`, `lat`, `lng` in body) |

### Favorites & ratings
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/user-favorites?festival_id=[&user_id=]` | Favorites for one user, or for everyone, on a festival |
| `POST` | `/api/user-favorites/toggle` | Toggle `isfavorite` for `(festival_id, user_id, set_id)` — **UPSERT** |
| `POST` | `/api/user-favorites/rate` | Set/clear a `notation` for `(festival_id, user_id, set_id)` — **UPSERT** |

### DJ tags
Collaborative, free-text tags on a set (keyed by `set_id`, like favorites/ratings). Any user can add a tag; everyone sees them, each rendered with its author's avatar. A tag is normalized server-side (no spaces, no leading `#`, lowercased); identity is `(festival_id, user_id, set_id, tag)`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/dj-tags?festival_id=[&set_id=]` | Tags for one set, or for the whole festival |
| `POST` | `/api/dj-tags` | Add a tag (`festival_id, user_id, set_id, tag` in body) — idempotent **UPSERT**, returns the normalized `tag` |
| `DELETE` | `/api/dj-tags` | Remove one's own tag (`festival_id, user_id, set_id, tag` in body) |

### Stages & geolocation
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stages?festival_id=` | All stages with their corner/rally-point coordinates and their map anchor (`map_anchor_x/y`, `map_exclusion_radius`) |
| `GET` | `/api/stages/<name>?festival_id=` | A single stage |
| `PUT` | `/api/stages/<name>` | Update a stage's geo-box and/or map anchor (`festival_id` in body) |
| `POST` | `/api/geoloc` | Store a user's GPS fix, resolve its stage, update presence (`festival_id` in body); response returns `stage` |

### Events (SOS / lost / hype)
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/events?festival_id=&user_id=` | A user's events on a festival, newest first |
| `POST` | `/api/events` | Create an event (`festival_id` in body); fans out a push and runs type-specific logic |
| `DELETE` | `/api/events/last?festival_id=&user_id=` | Undo a user's most recent event |

### Weather
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/weather?festival_id=` | Cached forecast for a festival's days |
| `POST` | `/update-weather` | Refresh forecasts for all upcoming/ongoing festivals from WeatherAPI |

### Scheduled pushes & Journal
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/push/tick[?festival_id=]` | Cron tick: sends any push due *now* (festival-local time) and logs it. Idempotent (dedup via `notifications.slot_key`). Without `festival_id`, processes all active festivals — one cron covers everything. |
| `GET` | `/api/journal?festival_id=` | The festival's notification journal (newest first) — feeds the in-app Journal screen |

### Line-up refresh (admin)
| Method | Endpoint | Description |
|---|---|---|
| `GET`/`POST` | `/api/admin/refresh-lineup[?festival_id=][&dry_run=true][&force=true]` | Re-scrape a festival's line-up and **diff-sync** it into `timetable` (stable `set_id`), log every change to the Journal (`programmation` theme) and push one notification per change. **Mutates data → requires a secret in a header** (`Authorization: Bearer <ADMIN_REFRESH_TOKEN>` or `X-Admin-Token: <…>`; never in the URL). Without `festival_id`, processes all active, not-yet-ended festivals that have a scraper. `dry_run=true` returns the planned changes **without writing or pushing**. `force=true` bypasses the mass-cancellation guard. |
| `GET`/`POST` | `/api/admin/rejournal-programmation[?festival_id=]` | Re-render the title/body of already-logged `programmation` journal entries with the **current wording** (retroactive; reads the set's current state from `timetable`). Idempotent, sends no push. Same header secret. |

---

## How the interesting bits work

- **Festival metadata, not constants.** City, dates and timezone come from the `festivals` table via `get_bigquery_festival()`. `/timetable` derives the UTC offset from the festival's IANA `timezone` (`zoneinfo`) instead of a hard-coded `+2h`.

- **Idempotent favorites via `MERGE`.** Toggle and rating writes use BigQuery `MERGE` (UPSERT): the first interaction inserts the `(festival_id, user_id, set_id)` row, later ones update it.

- **Stage resolution.** `/api/geoloc` stores the raw fix in the `geoloc` table, then checks the point against each stage's bounding box (computed from its four corner coordinates) and upserts the user's presence in `festival_users` (`last_location` = stage name).

- **Stage map anchor (single source of column list).** A stage also carries `map_anchor_x`/`map_anchor_y` (fractional 0-1 position on the mobile app's illustrated map) and `map_exclusion_radius` (zone kept free of avatar markers), calibrated by an admin from the app. These live alongside the geo-box columns in the same `_STAGE_COLS` list that drives `get_bigquery_stages`, `create_bigquery_stage` and `update_bigquery_stage` — the `UPDATE`'s `SET` clause is generated from that list rather than hard-coded, after a bug where a hard-coded clause silently ignored newly-added columns (200 OK, nothing written).

- **Event fan-out.** Creating an event writes to BigQuery and then triggers side effects by type:
  `sos` → high-priority push · `hype` → normal push · `lost` → re-resolve **every** user's stage on that festival (so the group can regroup) **then** push.
  Each real-time push is also **journaled** into `notifications` (so it shows in the in-app Journal), but **only during the festival window** (`start_date ≤ festival-local date ≤ end_date`) — nothing before or after. `lost` is journaled under theme `lost` (reusing the Journal icon); `sos`/`hype` under their own theme.

- **Hype message variety with no-repeat.** Real-time `hype` pushes pick from ~24 worded variants split across three tiers by available data: stage **+** now-playing DJ / stage only / neither (the author's name lives in the push *title*, so bodies stay fresh). Each variant has a stable key (`sd*`/`s*`/`p*`) stored in `notifications.variant`; the picker reads the cycle state from the journal (`count % P`) and won't reuse a variant until the whole tier has been shown once — robust across Render restarts/workers since the state is in BigQuery, not in memory.

- **Per-festival weather writes.** `/update-weather` no longer truncates the whole `weather` table (that would wipe other festivals). It deletes only the target festival's rows, then appends fresh ones tagged with `festival_id`.

- **Countdown pushes before the festival.** Same tick/Journal pipeline handles "J-N" milestones (`COUNTDOWN_DAYS` in `push_schedule.py`: 30/21/14/10/7/5/3/2/1 days before `start_date`), each fired once at `COUNTDOWN_TIME` local. Because they only depend on the date, they're testable live well before the event.

- **Scheduled pushes via a single "tick" cron.** Rather than dozens of cron entries (one per time slot), a single cron-job.org entry hits `/api/push/tick` every ~5 min. The schedule and message texts live in `push_schedule.py`; each tick reads the festival's **local** time (`zoneinfo`), figures which slots are due, computes the "winner" (most lost / biggest drinker / top-rated DJs of the day, etc.) from `events`/`user_favorites`, sends the FCM push to topic `all_users`, and logs it. The `notifications` table doubles as the **dedup guard** (`slot_key` unique per festival+date+slot) so re-ticks never double-send, and as the **Journal** source of truth — which also holds the real-time SOS/perdu/hype events (see *Event fan-out*), so the Journal mixes scheduled and live entries. Missed slots (cron stalled) are journaled without a late push; the day-after run sends one wrap-up push plus `pushed=false` leaderboard rows. Texts are gender-aware via the `users.gender` column.

- **Onboarding reminders (tent & location).** The same tick fires a few one-shot nudges (all in `push_schedule.py`). A **tent reminder** goes out on day 0, 30 min before the first set (time derived from the timetable), telling people to save their camp location. **Location-sharing reminders** run a graduated sequence — an opener the evening before (`LOCATION_PREDAY_TIME`) plus day-0 slots (`LOCATION_REMINDERS_DAY0`) whose argument escalates (find your stage → better notifications → be found if lost → SOS/safety). Both reuse the slot dedup + catch-up guard (a reminder missed by >`CATCH_UP_MIN` is journaled, not pushed late). They broadcast to `all_users`, so copy is inclusive ("if you haven't already").

- **Batch loads, not streaming, for events.** Event rows are written with **batch load jobs** rather than streaming inserts, because streamed rows are locked out of `DELETE`/`UPDATE` for ~90 min — which would break the "undo last event" endpoint.

- **Diff-based line-up sync (stable `set_id`).** `/api/admin/refresh-lineup` re-scrapes a festival and reconciles it with `timetable` by **natural key** `(dj, stage, day)` (b2b-order- and case-insensitive). A set keeps its `set_id` when only its time changes (so favorites, ratings and tags — all keyed by `set_id` — stay attached); a brand-new set gets a fresh id; a set that disappears is **soft-deleted** (`active=FALSE`, `deactivated_at`), not erased, so it can be reactivated (same id) if it reappears. Bios are scraped only for genuinely new sets — existing artists are never re-fetched or overwritten. Every visible change is logged to the Journal (`programmation` theme) and pushed (one per change). Any **new stage** seen in the scrape (a surprise area) is auto-added to the `stages` table with zero coordinates — additively, never touching the geo-boxes already set on existing stages. A **mass-cancellation guard** refuses to apply if a run would deactivate more than half the active sets (empty/broken scrape) unless `force=true` — important since the endpoint is cron-driven with no human reviewing the plan.

- **Credentials without files.** Google service-account credentials are read from an environment variable (`GOOGLE_APPLICATION_CREDENTIALS_JSON`) and materialized into a temp file at startup, so nothing sensitive is committed or baked into the image.

---

## Project Structure

```
extremalineup-api/
├── app.py                      # Flask app + all route handlers
├── bigquery.py                 # Data-access layer (every BQ query lives here)
├── firebase_cloud_messaging.py # SOS / lost / hype + generic topic push builders
├── push_schedule.py            # Scheduled pushes: daily schedule, texts, winner logic, palmarès
├── scrapers/                   # Line-up scrapers, one adapter per festival
│   ├── __init__.py             # SCRAPERS registry: festival_id → adapter
│   └── awakenings.py           # Awakenings 2026 timetable + bios scraper
├── config.py                   # Env-driven config (BQ table refs, keys)
├── migrations/
│   ├── 001_multi_festival.sql  # Schema migration: festivals table, festival_id, festival_users, district→stage rename
│   ├── 005_dj_tags.sql         # Collaborative DJ tags
│   ├── 007_push_journal.sql    # users.gender + notifications (journal + push dedup)
│   ├── 008_journal_realtime_events.sql  # notifications.variant (real-time events journal + hype dedup)
│   ├── 009_user_tent.sql       # festival_users.tent_lat/tent_lng (per-festival camp location)
│   ├── 010_timetable_soft_delete.sql  # timetable.active/deactivated_at (stable set_id on re-scrape)
│   ├── 011_stage_id.sql        # stages.stage_id / timetable.stage_id (stable stage identity)
│   ├── 012_stage_order.sql     # stages.stage_order (explicit admin display order)
│   └── 013_stage_map_anchor.sql  # stages.map_anchor_x/y + map_exclusion_radius, festival_users.last_seen_at
└── requirements.txt
```

---

## Getting Started

### Prerequisites
- Python 3.x
- A Google Cloud service account with BigQuery access
- A Firebase service account (for the Admin SDK)
- A WeatherAPI key

### Environment variables
| Variable | Purpose |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | Full service-account JSON (as a string) |
| `WEATHER_API_KEY` | WeatherAPI key |
| `ADMIN_REFRESH_TOKEN` | Shared secret guarding `POST/GET /api/admin/refresh-lineup` (line-up re-sync). Sent in a header only (`Authorization: Bearer …` or `X-Admin-Token: …`), compared in constant time. If unset, the endpoint refuses every request (fail-closed). |

### Run locally
```bash
pip install -r requirements.txt
# export the env vars above
python app.py            # dev server on 0.0.0.0:5000
# production:
gunicorn app:app
```

### Database migration
Apply `migrations/001_multi_festival.sql` in the BigQuery console to create the `festivals` / `festival_users` tables, add `festival_id` columns, backfill existing data, and rename `districts → stages` (`district → stage`, `stage → host`).

Apply `migrations/005_dj_tags.sql` to create the `dj_tags` table (collaborative tags on sets).

Apply `migrations/007_push_journal.sql` to add `users.gender` (fill `'m'`/`'f'` per user by hand) and create the `notifications` table (scheduled-push journal + dedup). Then set up **one** cron-job.org entry calling `GET /api/push/tick` every ~5 min.

Apply `migrations/008_journal_realtime_events.sql` to add `notifications.variant` (real-time SOS/perdu/hype journaling during the festival window + hype variant no-repeat). Required before deploying the change, otherwise the journaling `INSERT` fails.

Apply `migrations/009_user_tent.sql` to add `festival_users.tent_lat` / `tent_lng` (a user's tent/camp location per festival, set via `POST /users/<id>/tent` and read back in `/users`). Required before deploying, otherwise the tent upsert/read fails.

Apply `migrations/010_timetable_soft_delete.sql` to add `timetable.active` / `deactivated_at`. This switches the scraper from "delete + reassign every set_id" (which orphaned favorites/tags/ratings) to a diff-based sync that keeps `set_id` stable: a set keeps its id when only its time changes, and a removed set is **deactivated** (kept for history, hidden from the app) rather than erased. **Required before deploying** — `/timetable` now filters on `active`, so the column must exist first.

Apply `migrations/011_stage_id.sql` to add `stages.stage_id` / `timetable.stage_id` (stable numeric id for a stage, backfilled from the existing name match). Purely additive — `stage` (name) stays the key used everywhere else. **Required before deploying** the admin panel's stages CRUD (`POST`/`DELETE /api/stages`), otherwise stage creation fails on the missing column.

Apply `migrations/012_stage_order.sql` to add `stages.stage_order` (explicit display order set by the admin on the stage itself, takes priority over the order derived from `timetable.stage_order`). **Required before deploying** the stage edit endpoint (`PUT /api/stages/<name>/rename`), otherwise editing a stage's order fails on the missing column.

Apply `migrations/013_stage_map_anchor.sql` to add `stages.map_anchor_x` / `map_anchor_y` / `map_exclusion_radius` (a stage's calibrated position on the mobile app's illustrated map, and the radius kept free of avatar markers) and `festival_users.last_seen_at` (timestamp of a user's last GPS fix, shown as "seen N minutes ago" in the app's Map tab). **Required before deploying** the Map tab's calibration screen and the updated `PUT /api/stages/<name>` — without it, `update_bigquery_stage`'s generated `SET` clause references columns that don't exist yet.

---

## Notes

- The BigQuery dataset/table layout is configured in `config.py` (`festivals`, `festival_users`, `timetable`, `users`, `user_favorites`, `dj_tags`, `stages`, `geoloc`, `events`, `weather`).
- Festival days, city and timezone are no longer hard-coded — they come from the `festivals` table.
- Push notifications currently use a single global `all_users` topic. Per-festival topics are a planned improvement.
- CORS is open for the mobile client.

---

Part of the **FestCompanion** project — see the [mobile app README](../../FestCompanion/festcompanion) for the full system overview.
