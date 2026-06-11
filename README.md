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
| `festival_users` | per-festival | Live presence: `(festival_id, user_id, last_lat, last_lng, last_location)`. |
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
| `GET` | `/users?festival_id=` | Users present on a festival (id, username, phone, last location & stage, role) |
| `GET` | `/users/check?username=` | Resolve a username to its user id (global, no `festival_id`) |
| `POST` | `/users/<id>/phone` | Update a user's phone number (global) |
| `POST` | `/users/<id>/location` | Update a user's coordinates on a festival (`festival_id` in body) |

### Favorites & ratings
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/user-favorites?festival_id=[&user_id=]` | Favorites for one user, or for everyone, on a festival |
| `POST` | `/api/user-favorites/toggle` | Toggle `isfavorite` for `(festival_id, user_id, set_id)` — **UPSERT** |
| `POST` | `/api/user-favorites/rate` | Set/clear a `notation` for `(festival_id, user_id, set_id)` — **UPSERT** |

### Stages & geolocation
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stages?festival_id=` | All stages with their corner/rally-point coordinates |
| `GET` | `/api/stages/<name>?festival_id=` | A single stage |
| `PUT` | `/api/stages/<name>` | Update a stage's geo-box (`festival_id` in body) |
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

---

## How the interesting bits work

- **Festival metadata, not constants.** City, dates and timezone come from the `festivals` table via `get_bigquery_festival()`. `/timetable` derives the UTC offset from the festival's IANA `timezone` (`zoneinfo`) instead of a hard-coded `+2h`.

- **Idempotent favorites via `MERGE`.** Toggle and rating writes use BigQuery `MERGE` (UPSERT): the first interaction inserts the `(festival_id, user_id, set_id)` row, later ones update it.

- **Stage resolution.** `/api/geoloc` stores the raw fix in the `geoloc` table, then checks the point against each stage's bounding box (computed from its four corner coordinates) and upserts the user's presence in `festival_users` (`last_location` = stage name).

- **Event fan-out.** Creating an event writes to BigQuery and then triggers side effects by type:
  `sos` → high-priority push · `hype` → normal push · `lost` → re-resolve **every** user's stage on that festival (so the group can regroup) **then** push.

- **Per-festival weather writes.** `/update-weather` no longer truncates the whole `weather` table (that would wipe other festivals). It deletes only the target festival's rows, then appends fresh ones tagged with `festival_id`.

- **Batch loads, not streaming, for events.** Event rows are written with **batch load jobs** rather than streaming inserts, because streamed rows are locked out of `DELETE`/`UPDATE` for ~90 min — which would break the "undo last event" endpoint.

- **Credentials without files.** Google service-account credentials are read from an environment variable (`GOOGLE_APPLICATION_CREDENTIALS_JSON`) and materialized into a temp file at startup, so nothing sensitive is committed or baked into the image.

---

## Project Structure

```
extremalineup-api/
├── app.py                      # Flask app + all route handlers
├── bigquery.py                 # Data-access layer (every BQ query lives here)
├── firebase_cloud_messaging.py # SOS / lost / hype push builders
├── config.py                   # Env-driven config (BQ table refs, keys)
├── migrations/
│   └── 001_multi_festival.sql  # Schema migration: festivals table, festival_id, festival_users, district→stage rename
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

---

## Notes

- The BigQuery dataset/table layout is configured in `config.py` (`festivals`, `festival_users`, `timetable`, `users`, `user_favorites`, `stages`, `geoloc`, `events`, `weather`).
- Festival days, city and timezone are no longer hard-coded — they come from the `festivals` table.
- Push notifications currently use a single global `all_users` topic. Per-festival topics are a planned improvement.
- CORS is open for the mobile client.

---

Part of the **FestCompanion** project — see the [mobile app README](../../FestCompanion/festcompanion) for the full system overview.
