import logging
import datetime
import re
from collections import defaultdict
from google.cloud import bigquery
from google.oauth2 import service_account
import pandas as pd
from config import Config

def get_google_credentials(credentials_path=None):
    credentials_path = credentials_path or Config.GOOGLE_APPLICATION_CREDENTIALS
    return service_account.Credentials.from_service_account_file(credentials_path)

credentials = get_google_credentials()
client = bigquery.Client(project=Config.BQ_PROJECT, credentials=credentials)


# ============================================================================
# FESTIVALS (métadonnées — remplacent les constantes codées en dur)
# ============================================================================

def _festival_row_to_dict(row):
    """Convertit une ligne `festivals` en dict JSON-sérialisable."""
    return {
        "festival_id": int(row.festival_id),
        "slug": str(row.slug) if row.slug is not None else None,
        "name": str(row.name) if row.name is not None else None,
        "city": str(row.city) if row.city is not None else None,
        "country": str(row.country) if row.country is not None else None,
        "start_date": row.start_date.isoformat() if row.start_date is not None else None,
        "end_date": row.end_date.isoformat() if row.end_date is not None else None,
        "timezone": str(row.timezone) if row.timezone is not None else None,
        "is_active": bool(row.is_active) if row.is_active is not None else False,
        "parking": str(row.parking) if row.parking is not None else None,
    }

def get_bigquery_festivals(active_only=True):
    """Récupère la liste des festivals (pour l'écran de sélection)."""
    try:
        where = "WHERE is_active = TRUE" if active_only else ""
        query = f"""
        SELECT festival_id, slug, name, city, country, start_date, end_date, timezone, is_active, parking
        FROM `{Config.BQ_FESTIVALS}`
        {where}
        ORDER BY start_date DESC
        """
        rows = client.query(query).result()
        return [_festival_row_to_dict(row) for row in rows]
    except Exception as e:
        logging.error(f"Erreur get_bigquery_festivals: {e}")
        raise

def get_bigquery_festival(festival_id):
    """Récupère un festival par son id. Retourne None si introuvable."""
    try:
        query = f"""
        SELECT festival_id, slug, name, city, country, start_date, end_date, timezone, is_active, parking
        FROM `{Config.BQ_FESTIVALS}`
        WHERE festival_id = @festival_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return _festival_row_to_dict(rows[0]) if rows else None
    except Exception as e:
        logging.error(f"Erreur get_bigquery_festival: {e}")
        raise


# ============================================================================
# TIMETABLE
# ============================================================================

def get_bigquery_timetable(festival_id):
    """Récupère la timetable d'un festival sous forme de DataFrame.
    Colonnes de scène : `stage` (lieu géolocalisé) et `host` (collectif)."""
    try:
        # IFNULL(active, TRUE) : ne renvoie que les sets actifs, tout en restant
        # compatible avec les lignes antérieures à la migration 010 (active NULL).
        query = f"""
        SELECT * FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id AND IFNULL(active, TRUE) = TRUE
        ORDER BY day_int, stage, start_time
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        df = client.query(query, job_config=job_config).result().to_dataframe()
        # Colonnes internes au soft-delete : inutiles à l'app et `deactivated_at`
        # (NaT pour les sets actifs) ferait échouer jsonify (non géré par _nan_to_none).
        return df.drop(columns=["active", "deactivated_at"], errors="ignore")
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la timetable: {e}")
        raise


# ============================================================================
# SYNCHRONISATION DE LA TIMETABLE (diff/merge — set_id STABLE)
# ============================================================================
# Identité d'un set = clé naturelle NK = (dj, stage, day) NORMALISÉE. Seul
# l'horaire qui change conserve le set_id (UPDATE). Un set disparu du scrape est
# DÉSACTIVÉ (active=FALSE), pas supprimé : ses favoris/tags/notes restent valides
# et il peut être rétabli (réactivation du MÊME set_id) s'il réapparaît.
# La bio n'est écrite qu'à la CRÉATION (INSERT) — jamais réécrite sur un set
# existant.

_TIMETABLE_SCHEMA = [
    bigquery.SchemaField("set_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("dj", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("stage", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("host", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("day", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("day_int", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("stage_order", "INT64"),
    bigquery.SchemaField("bio", "STRING"),
    bigquery.SchemaField("start_time", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("end_time", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("festival_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("active", "BOOL"),
    bigquery.SchemaField("deactivated_at", "TIMESTAMP"),
]

# Garde-fou anti-désactivation massive : un scrape vide/cassé/partiel ne doit
# JAMAIS pouvoir annuler une grande partie du line-up (crucial en cron, sans
# humain pour relire le plan). Au-delà de ce seuil, on refuse d'appliquer.
_MAX_CANCEL_FRACTION = 0.5

# Sépare les artistes d'un b2b pour rendre la clé insensible à leur ordre.
_SEP_RE = re.compile(r"\s*(?:&|\bb2b\b)\s*", re.IGNORECASE)

_PROGRAMMATION_THEME = "programmation"

_FR_DAYS = {
    "monday": "lundi", "tuesday": "mardi", "wednesday": "mercredi",
    "thursday": "jeudi", "friday": "vendredi", "saturday": "samedi",
    "sunday": "dimanche",
}


class MassCancellationError(RuntimeError):
    """Levée quand un apply annulerait trop de sets actifs (scrape suspect)."""


def _normalize_artist(dj) -> str:
    parts = [p.strip().casefold() for p in _SEP_RE.split(str(dj)) if p.strip()]
    return " & ".join(sorted(parts)) if parts else str(dj).strip().casefold()


def _nk(dj, stage, day) -> tuple:
    return (_normalize_artist(dj), str(stage).strip().casefold(), str(day).strip().casefold())


def _to_naive_utc(ts):
    """datetime/Timestamp (aware ou naïf) -> datetime naïf en UTC."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return ts


def _opt_int(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return int(v)


def _opt_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v)


def get_max_set_id() -> int:
    """Plus grand set_id existant (tous festivals, actifs ET inactifs), ou -1 si
    table vide. Garantit des set_id globalement uniques et jamais réutilisés."""
    query = f"SELECT MAX(set_id) AS m FROM `{Config.BQ_TIMETABLE}`"
    rows = list(client.query(query).result())
    return rows[0].m if rows and rows[0].m is not None else -1


def _load_existing_sets(festival_id):
    """Tous les sets d'un festival (actifs ET inactifs), pour le diff."""
    query = f"""
        SELECT set_id, dj, stage, host, day, day_int, stage_order, bio,
               start_time, end_time, IFNULL(active, TRUE) AS active
        FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
    )
    rows = client.query(query, job_config=job_config).result()
    return [
        {
            "set_id": int(r.set_id), "dj": r.dj, "stage": r.stage, "host": r.host,
            "day": r.day, "day_int": r.day_int, "stage_order": r.stage_order, "bio": r.bio,
            "start_time": _to_naive_utc(r.start_time),
            "end_time": _to_naive_utc(r.end_time),
            "active": bool(r.active),
        }
        for r in rows
    ]


def select_new_sets(df, festival_id):
    """Masque booléen des lignes de `df` ABSENTES de la base (sets jamais vus,
    actifs ou inactifs) — pour n'enrichir bio/photo QUE les nouveaux. Un set
    rétabli n'est PAS « nouveau » (sa bio d'origine est conservée)."""
    known = {_nk(r["dj"], r["stage"], r["day"]) for r in _load_existing_sets(festival_id)}
    return df.apply(lambda r: _nk(r["dj"], r["stage"], r["day"]) not in known, axis=1)


def sync_timetable_festival(df, festival_id, dry_run=False, force=False):
    """Synchronise la timetable d'UN festival par diff (set_id STABLE).
    Retourne la liste des changements (types : added / rescheduled / restored /
    cancelled / updated). dry_run=True : ne calcule que le plan, n'écrit rien.
    force=False : lève MassCancellationError si > _MAX_CANCEL_FRACTION des sets
    actifs seraient désactivés (scrape suspect)."""
    try:
        df = df.copy()
        df["start_time"] = df["start_time"].apply(_to_naive_utc)
        df["end_time"] = df["end_time"].apply(_to_naive_utc)

        existing = _load_existing_sets(festival_id)
        by_nk = defaultdict(list)
        for r in existing:
            by_nk[_nk(r["dj"], r["stage"], r["day"])].append(r)
        for lst in by_nk.values():
            lst.sort(key=lambda r: r["start_time"] or datetime.datetime.min)

        matched = set()
        changes = []
        insert_rows = []
        update_ops = []
        next_id = get_max_set_id() + 1

        for _, row in df.iterrows():
            nk = _nk(row["dj"], row["stage"], row["day"])
            candidates = [r for r in by_nk.get(nk, []) if r["set_id"] not in matched]
            if candidates:
                match = min(
                    candidates,
                    key=lambda r: abs(((r["start_time"] or datetime.datetime.min) - row["start_time"]).total_seconds()),
                )
                matched.add(match["set_id"])
                reactivate = not match["active"]
                time_changed = (
                    match["start_time"] != row["start_time"]
                    or match["end_time"] != row["end_time"]
                )
                attrs_changed = (
                    (_opt_str(match["host"]) or "") != (_opt_str(row.get("host")) or "")
                    or match["stage_order"] != _opt_int(row.get("stage_order"))
                    or str(match["dj"]) != str(row["dj"])
                    or str(match["stage"]) != str(row["stage"])
                    or str(match["day"]) != str(row["day"])
                )
                if reactivate or time_changed or attrs_changed:
                    update_ops.append((match["set_id"], row, reactivate))
                if reactivate:
                    changes.append(_change("restored", match["set_id"], row,
                                           new_start=row["start_time"], new_end=row["end_time"]))
                elif time_changed:
                    changes.append(_change(
                        "rescheduled", match["set_id"], row,
                        old_start=match["start_time"], new_start=row["start_time"],
                        old_end=match["end_time"], new_end=row["end_time"],
                    ))
                elif attrs_changed:
                    changes.append(_change("updated", match["set_id"], row))
            else:
                sid = next_id
                next_id += 1
                insert_rows.append((sid, row))
                changes.append(_change("added", sid, row,
                                       new_start=row["start_time"], new_end=row["end_time"]))

        cancelled_ids = [r["set_id"] for r in existing if r["active"] and r["set_id"] not in matched]
        for r in existing:
            if r["active"] and r["set_id"] not in matched:
                changes.append({
                    "type": "cancelled", "set_id": r["set_id"], "dj": r["dj"],
                    "stage": r["stage"], "day": r["day"], "old_start": r["start_time"],
                })

        if dry_run:
            return changes

        active_count = sum(1 for r in existing if r["active"])
        if not force and active_count and len(cancelled_ids) > active_count * _MAX_CANCEL_FRACTION:
            raise MassCancellationError(
                f"{len(cancelled_ids)}/{active_count} sets actifs seraient désactivés "
                f"(> {int(_MAX_CANCEL_FRACTION * 100)} %). Scrape probablement vide/cassé/"
                f"partiel — application ANNULÉE (rien écrit). Relancer avec force=True si "
                f"c'est un vrai remaniement."
            )

        if insert_rows:
            _insert_new_sets(insert_rows, festival_id)
        for set_id, row, reactivate in update_ops:
            _update_set(set_id, row, festival_id, reactivate)
        if cancelled_ids:
            _deactivate_sets(cancelled_ids, festival_id)

        return changes
    except MassCancellationError:
        raise
    except Exception as e:
        logging.error(f"Erreur sync_timetable_festival: {e}")
        raise


def _change(change_type, set_id, row, **extra):
    base = {"type": change_type, "set_id": set_id, "dj": row["dj"],
            "stage": row["stage"], "day": row["day"]}
    base.update(extra)
    return base


def _insert_new_sets(insert_rows, festival_id):
    recs = [
        {
            "set_id": set_id, "dj": str(row["dj"]), "stage": str(row["stage"]),
            "host": _opt_str(row.get("host")) or "", "day": str(row["day"]),
            "day_int": int(row["day_int"]), "stage_order": _opt_int(row.get("stage_order")),
            "bio": _opt_str(row.get("bio")),
            "start_time": row["start_time"], "end_time": row["end_time"],
            "festival_id": festival_id, "active": True, "deactivated_at": None,
        }
        for set_id, row in insert_rows
    ]
    new_df = pd.DataFrame(recs)[[f.name for f in _TIMETABLE_SCHEMA]]
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", schema=_TIMETABLE_SCHEMA)
    client.load_table_from_dataframe(new_df, Config.BQ_TIMETABLE, job_config=job_config).result()


def _update_set(set_id, row, festival_id, reactivate):
    """UPDATE des attributs (HORS bio) d'un set existant — set_id préservé. La bio
    n'est jamais réécrite (posée à la création)."""
    extra = ", active = TRUE, deactivated_at = NULL" if reactivate else ""
    query = f"""
        UPDATE `{Config.BQ_TIMETABLE}`
        SET dj = @dj, stage = @stage, host = @host, day = @day, day_int = @day_int,
            stage_order = @stage_order,
            start_time = @start_time, end_time = @end_time{extra}
        WHERE festival_id = @festival_id AND set_id = @set_id
    """
    params = [
        bigquery.ScalarQueryParameter("dj", "STRING", str(row["dj"])),
        bigquery.ScalarQueryParameter("stage", "STRING", str(row["stage"])),
        bigquery.ScalarQueryParameter("host", "STRING", _opt_str(row.get("host")) or ""),
        bigquery.ScalarQueryParameter("day", "STRING", str(row["day"])),
        bigquery.ScalarQueryParameter("day_int", "INT64", int(row["day_int"])),
        bigquery.ScalarQueryParameter("stage_order", "INT64", _opt_int(row.get("stage_order"))),
        bigquery.ScalarQueryParameter("start_time", "TIMESTAMP", row["start_time"]),
        bigquery.ScalarQueryParameter("end_time", "TIMESTAMP", row["end_time"]),
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
    ]
    client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()


def _deactivate_sets(set_ids, festival_id):
    query = f"""
        UPDATE `{Config.BQ_TIMETABLE}`
        SET active = FALSE, deactivated_at = CURRENT_TIMESTAMP()
        WHERE festival_id = @festival_id AND set_id IN UNNEST(@ids)
    """
    params = [
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ArrayQueryParameter("ids", "INT64", list(set_ids)),
    ]
    client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()


# --- Journal des changements de programmation (theme = 'programmation') ------

def _fr_day(day) -> str:
    return _FR_DAYS.get(str(day).strip().lower(), str(day))


def _local_hm(dt, tz) -> str:
    if dt is None:
        return ""
    return dt.replace(tzinfo=datetime.timezone.utc).astimezone(tz).strftime("%H:%M")


def _lineup_change_message(c, tz):
    """(title, body) lisibles festivalier, ou None si le type n'est pas journalisé."""
    dj, stage, day = c["dj"], c["stage"], _fr_day(c["day"])
    start = _local_hm(c.get("new_start"), tz)
    end = _local_hm(c.get("new_end"), tz)
    if c["type"] == "added":
        return ("Nouveau DJ ajouté au line-up ! 🎉",
                f"{dj} débarque sur {stage}, {day} de {start} à {end}.")
    if c["type"] == "restored":
        return ("Finalement, si ! ✅",
                f"{dj} est maintenu sur {stage}, {day} de {start} à {end}.")
    if c["type"] == "cancelled":
        return ("Set annulé… ❌",
                f"{dj} ne jouera finalement pas ({stage}, {day}).")
    if c["type"] == "rescheduled":
        return ("Nouvel horaire 🕒",
                f"Le set de {dj} aura maintenant lieu {day} de {start} à {end} sur {stage}.")
    return None


def journal_lineup_changes(festival_id, changes, tz) -> int:
    """Consigne les changements VISIBLES dans `notifications` (theme=
    'programmation', pushed=FALSE). Retourne le nombre d'entrées écrites."""
    run_ts = datetime.datetime.utcnow().isoformat()
    written = 0
    for c in changes:
        msg = _lineup_change_message(c, tz)
        if msg is None:
            continue
        title, body = msg
        slot_key = f"prog|{c['type']}|{c['set_id']}|{run_ts}"
        insert_notification(
            festival_id, slot_key, day=(str(c.get("day")) if c.get("day") else None),
            scheduled_local=None, theme=_PROGRAMMATION_THEME,
            title=title, body=body, pushed=False, variant=None,
        )
        written += 1
    return written


def _get_set_by_id(festival_id, set_id):
    """État courant d'un set (dj/scène/jour/horaires) par son set_id — actif ou
    non. Sert à régénérer le texte d'une entrée de journal déjà écrite."""
    query = f"""
        SELECT dj, stage, day, start_time, end_time
        FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id AND set_id = @set_id
        LIMIT 1
    """
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
    ])
    rows = list(client.query(query, job_config=jc).result())
    if not rows:
        return None
    r = rows[0]
    return {
        "dj": r.dj, "stage": r.stage, "day": r.day,
        "start_time": _to_naive_utc(r.start_time),
        "end_time": _to_naive_utc(r.end_time),
    }


def _update_notification_text(festival_id, slot_key, title, body):
    query = f"""
        UPDATE `{Config.BQ_NOTIFICATIONS}` SET title = @title, body = @body
        WHERE festival_id = @festival_id AND slot_key = @slot_key
    """
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("title", "STRING", title),
        bigquery.ScalarQueryParameter("body", "STRING", body),
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("slot_key", "STRING", slot_key),
    ])
    client.query(query, job_config=jc).result()


def rejournal_programmation(festival_id, tz):
    """RÉTROACTIF : régénère titre+corps des entrées 'programmation' déjà
    journalisées, à partir du wording COURANT et de l'état actuel du set
    (timetable). Idempotent (réécrit au même texte si relancé). Le set_id et le
    type sont relus dans le slot_key (`prog|type|set_id|run_ts`). Retourne le
    nombre d'entrées mises à jour. N'envoie aucun push."""
    q = f"""
        SELECT slot_key FROM `{Config.BQ_NOTIFICATIONS}`
        WHERE festival_id = @festival_id AND theme = @theme
    """
    jc = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("theme", "STRING", _PROGRAMMATION_THEME),
    ])
    updated = 0
    for row in client.query(q, job_config=jc).result():
        slot_key = str(row.slot_key)
        parts = slot_key.split("|")
        if len(parts) < 3 or parts[0] != "prog":
            continue
        ctype = parts[1]
        try:
            set_id = int(parts[2])
        except ValueError:
            continue
        s = _get_set_by_id(festival_id, set_id)
        if not s:
            continue
        # On régénère depuis l'état COURANT du set. Le nouveau wording n'utilise
        # que les horaires actuels (new_start/new_end) — pas les anciens.
        change = {
            "type": ctype, "dj": s["dj"], "stage": s["stage"], "day": s["day"],
            "new_start": s["start_time"], "new_end": s["end_time"],
        }
        msg = _lineup_change_message(change, tz)
        if msg is None:
            continue
        title, body = msg
        _update_notification_text(festival_id, slot_key, title, body)
        updated += 1
    return updated


def get_unpushed_programmation(festival_id):
    """Entrées de programmation pas encore poussées (pour le push 2b, idempotent :
    une push échouée repart au run suivant)."""
    query = f"""
        SELECT slot_key, title, body
        FROM `{Config.BQ_NOTIFICATIONS}`
        WHERE festival_id = @festival_id AND theme = @theme AND IFNULL(pushed, FALSE) = FALSE
        ORDER BY created_at
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("theme", "STRING", _PROGRAMMATION_THEME),
    ])
    rows = client.query(query, job_config=job_config).result()
    return [{"slot_key": str(r.slot_key), "title": str(r.title), "body": str(r.body)} for r in rows]


def mark_notification_pushed(festival_id, slot_key):
    query = f"""
        UPDATE `{Config.BQ_NOTIFICATIONS}` SET pushed = TRUE
        WHERE festival_id = @festival_id AND slot_key = @slot_key
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
        bigquery.ScalarQueryParameter("slot_key", "STRING", slot_key),
    ])
    client.query(query, job_config=job_config).result()


def ensure_stages_exist(festival_id, stage_names):
    """Crée les scènes MANQUANTES d'un festival (additif, NON destructif) :
    coordonnées initialisées à 0 (à régler ensuite dans l'app). Les scènes
    existantes — et leurs coordonnées déjà réglées sur place — sont PRÉSERVÉES.
    Permet à une scène surprise apparue au scrape d'avoir sa ligne `stages`
    automatiquement. Retourne le nombre de scènes créées."""
    q = f"SELECT stage FROM `{Config.BQ_STAGES}` WHERE festival_id = @festival_id"
    jc = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
    )
    existing = {r.stage for r in client.query(q, job_config=jc).result()}
    missing = [n for n in dict.fromkeys(stage_names) if n and n not in existing]
    if not missing:
        return 0
    rows = [
        {"stage": name, "festival_id": festival_id, **{c: 0.0 for c in _STAGE_COLS}}
        for name in missing
    ]
    schema = (
        [bigquery.SchemaField("stage", "STRING", mode="REQUIRED")]
        + [bigquery.SchemaField(c, "FLOAT64") for c in _STAGE_COLS]
        + [bigquery.SchemaField("festival_id", "INT64", mode="REQUIRED")]
    )
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
    )
    client.load_table_from_json(rows, Config.BQ_STAGES, job_config=job_config).result()
    return len(missing)


# ============================================================================
# FAVORIS / NOTATIONS
# ============================================================================

def get_bigquery_user_favorites(festival_id, user_id=None):
    """
    Récupère les favoris d'un festival depuis BigQuery.
    - Si user_id est fourni : retourne [{"set_id", "isfavorite", "notation"}, ...] pour CET utilisateur
    - Si user_id est None : retourne [{"user_id", "set_id", "isfavorite", "notation"}, ...] pour TOUS les utilisateurs
    """
    try:
        if user_id is not None:
            query = f"""
            SELECT set_id, isfavorite, notation
            FROM `{Config.BQ_USER_FAVORITES}`
            WHERE festival_id = @festival_id AND user_id = @user_id
            ORDER BY set_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                ]
            )
            rows = client.query(query, job_config=job_config).result()
            return [
                {
                    "set_id": int(row.set_id),
                    "isfavorite": bool(row.isfavorite),
                    "notation": int(row.notation) if row.notation is not None else None,
                }
                for row in rows
            ]
        else:
            query = f"""
            SELECT user_id, set_id, isfavorite, notation
            FROM `{Config.BQ_USER_FAVORITES}`
            WHERE festival_id = @festival_id
            ORDER BY user_id, set_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
            )
            rows = client.query(query, job_config=job_config).result()
            return [
                {
                    "user_id": int(row.user_id),
                    "set_id": int(row.set_id),
                    "isfavorite": bool(row.isfavorite),
                    "notation": int(row.notation) if row.notation is not None else None,
                }
                for row in rows
            ]
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des favoris: {e}")
        raise

def toggle_bigquery_user_favorite(festival_id, user_id, set_id):
    """
    Toggle isfavorite pour (festival_id, user_id, set_id). MERGE (UPSERT).
    Retourne la nouvelle valeur de isfavorite.
    """
    try:
        select_query = f"""
        SELECT isfavorite
        FROM `{Config.BQ_USER_FAVORITES}`
        WHERE festival_id = @festival_id AND user_id = @user_id AND set_id = @set_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
            ]
        )
        rows = list(client.query(select_query, job_config=job_config).result())
        current_value = rows[0].isfavorite if rows else False
        new_value = not current_value

        merge_query = f"""
        MERGE `{Config.BQ_USER_FAVORITES}` AS target
        USING (SELECT @festival_id AS festival_id, @user_id AS user_id, @set_id AS set_id) AS source
        ON target.festival_id = source.festival_id
           AND target.user_id = source.user_id
           AND target.set_id = source.set_id
        WHEN MATCHED THEN
            UPDATE SET isfavorite = @new_value
        WHEN NOT MATCHED THEN
            INSERT (festival_id, user_id, set_id, isfavorite, notation)
            VALUES (@festival_id, @user_id, @set_id, @new_value, NULL)
        """
        merge_job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                bigquery.ScalarQueryParameter("new_value", "BOOL", new_value),
            ]
        )
        client.query(merge_query, job_config=merge_job_config).result()
        return new_value
    except Exception as e:
        logging.error(f"Erreur lors du toggle favori: {e}")
        raise

def update_bigquery_user_favorite_notation(festival_id, user_id, set_id, notation):
    """
    Met à jour la notation pour (festival_id, user_id, set_id). MERGE (UPSERT).
    notation peut être None pour remettre la note à NULL.
    """
    try:
        if notation is None:
            merge_query = f"""
            MERGE `{Config.BQ_USER_FAVORITES}` AS target
            USING (SELECT @festival_id AS festival_id, @user_id AS user_id, @set_id AS set_id) AS source
            ON target.festival_id = source.festival_id
               AND target.user_id = source.user_id
               AND target.set_id = source.set_id
            WHEN MATCHED THEN
                UPDATE SET notation = NULL
            WHEN NOT MATCHED THEN
                INSERT (festival_id, user_id, set_id, isfavorite, notation)
                VALUES (@festival_id, @user_id, @set_id, FALSE, NULL)
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                    bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                ]
            )
        else:
            merge_query = f"""
            MERGE `{Config.BQ_USER_FAVORITES}` AS target
            USING (SELECT @festival_id AS festival_id, @user_id AS user_id, @set_id AS set_id) AS source
            ON target.festival_id = source.festival_id
               AND target.user_id = source.user_id
               AND target.set_id = source.set_id
            WHEN MATCHED THEN
                UPDATE SET notation = @notation
            WHEN NOT MATCHED THEN
                INSERT (festival_id, user_id, set_id, isfavorite, notation)
                VALUES (@festival_id, @user_id, @set_id, FALSE, @notation)
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                    bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                    bigquery.ScalarQueryParameter("notation", "INT64", notation),
                ]
            )
        client.query(merge_query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la notation: {e}")
        raise


# ============================================================================
# TAGS DJ (collaboratifs, rattachés au set_id comme les favoris/notes)
# ============================================================================

def normalize_tag(raw):
    """Normalise un tag : sans espace, sans « # » de tête, minuscule.
    Règle partagée avec le frontend (Dart `DjTag.normalize`). Retourne '' si
    le tag est vide après nettoyage (l'appelant rejette alors la requête)."""
    if raw is None:
        return ""
    tag = str(raw).strip()
    if tag.startswith("#"):
        tag = tag[1:]
    # Supprime tous les caractères d'espacement (espaces, tabs, sauts de ligne).
    tag = "".join(tag.split())
    return tag.lower()


def get_bigquery_dj_tags(festival_id, set_id=None):
    """Récupère les tags d'un festival.
    - set_id fourni : tags de CE set → [{"user_id", "set_id", "tag"}, ...]
    - set_id None  : TOUS les tags du festival (alimente le cache + la page
      « DJ par tag ») → [{"user_id", "set_id", "tag"}, ...]
    """
    try:
        if set_id is not None:
            query = f"""
            SELECT user_id, set_id, tag
            FROM `{Config.BQ_DJ_TAGS}`
            WHERE festival_id = @festival_id AND set_id = @set_id
            ORDER BY tag, user_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                    bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                ]
            )
        else:
            query = f"""
            SELECT user_id, set_id, tag
            FROM `{Config.BQ_DJ_TAGS}`
            WHERE festival_id = @festival_id
            ORDER BY tag, set_id, user_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
            )
        rows = client.query(query, job_config=job_config).result()
        return [
            {"user_id": int(row.user_id), "set_id": int(row.set_id), "tag": str(row.tag)}
            for row in rows
        ]
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des tags: {e}")
        raise


def add_bigquery_dj_tag(festival_id, user_id, set_id, tag):
    """Ajoute un tag (festival_id, user_id, set_id, tag). MERGE → idempotent :
    un même utilisateur ne crée pas de doublon sur le même set. DML (pas de
    streaming) → la ligne est immédiatement supprimable."""
    try:
        merge_query = f"""
        MERGE `{Config.BQ_DJ_TAGS}` AS target
        USING (
            SELECT @festival_id AS festival_id, @user_id AS user_id,
                   @set_id AS set_id, @tag AS tag
        ) AS source
        ON target.festival_id = source.festival_id
           AND target.user_id = source.user_id
           AND target.set_id = source.set_id
           AND target.tag = source.tag
        WHEN NOT MATCHED THEN
            INSERT (festival_id, user_id, set_id, tag)
            VALUES (@festival_id, @user_id, @set_id, @tag)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                bigquery.ScalarQueryParameter("tag", "STRING", tag),
            ]
        )
        client.query(merge_query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur lors de l'ajout du tag: {e}")
        raise


def delete_bigquery_dj_tag(festival_id, user_id, set_id, tag):
    """Supprime le tag d'un utilisateur sur un set. Le user_id dans le WHERE
    garantit qu'on ne supprime QUE son propre tag."""
    try:
        query = f"""
        DELETE FROM `{Config.BQ_DJ_TAGS}`
        WHERE festival_id = @festival_id
          AND user_id = @user_id
          AND set_id = @set_id
          AND tag = @tag
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id),
                bigquery.ScalarQueryParameter("tag", "STRING", tag),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur lors de la suppression du tag: {e}")
        raise


# ============================================================================
# UTILISATEURS (comptes globaux, partagés entre festivals)
# ============================================================================

def get_bigquery_user_id(username):
    """Récupère l'ID d'un utilisateur depuis son username (STRING). Retourne None si introuvable."""
    try:
        query = f"SELECT id FROM `{Config.BQ_USERS}` WHERE username = @username"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("username", "STRING", username)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return rows[0].id if rows else None
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de l'user_id: {e}")
        raise

def bigquery_user_exists(username):
    """Vérifie si un utilisateur existe dans la table users (via son username)."""
    try:
        query = f"SELECT COUNT(*) AS count FROM `{Config.BQ_USERS}` WHERE username = @username"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("username", "STRING", username)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return rows[0].count > 0
    except Exception as e:
        logging.error(f"Erreur lors de la vérification de l'utilisateur: {e}")
        raise

def update_bigquery_user_phone(user_id, phone_number):
    """Met à jour le numéro de téléphone d'un utilisateur (donnée globale)."""
    try:
        query = f"UPDATE `{Config.BQ_USERS}` SET phone_number = @phone WHERE id = @user_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("phone", "STRING", phone_number),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            ]
        )
        client.query(query, job_config=job_config).result()
        logging.info(f"Numéro de téléphone mis à jour pour l'utilisateur {user_id}.")
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour du numéro de téléphone: {e}")
        raise

def get_username_by_id(user_id):
    """Récupère le username d'un utilisateur."""
    try:
        query = f"SELECT username FROM `{Config.BQ_USERS}` WHERE id = @user_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("user_id", "INT64", user_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return rows[0].username if rows else "Utilisateur inconnu"
    except Exception as e:
        logging.error(f"Erreur get_username_by_id: {str(e)}")
        return "Utilisateur inconnu"

def get_user_current_stage(festival_id, user_id):
    """Scène courante d'un utilisateur sur un festival (last_location de
    festival_users). Retourne None si inconnue ('?' ou vide). Best-effort."""
    try:
        query = f"""
        SELECT last_location
        FROM `{Config.BQ_FESTIVAL_USERS}`
        WHERE festival_id = @festival_id AND user_id = @user_id
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
        ])
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return None
        loc = rows[0].last_location
        if loc is None or str(loc).strip() in ("", "?"):
            return None
        return str(loc)
    except Exception as e:
        logging.error(f"Erreur get_user_current_stage: {e}")
        return None

def get_now_playing_dj(festival_id, stage):
    """DJ jouant actuellement sur une scène (timetable). Best-effort → None si
    rien/erreur. Les heures sont stockées en UTC ; on compare à l'instant UTC.
    TIMESTAMP(...) rend la comparaison robuste que la colonne soit DATETIME ou
    TIMESTAMP."""
    try:
        if not stage:
            return None
        query = f"""
        SELECT dj
        FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id
          AND IFNULL(active, TRUE) = TRUE
          AND stage = @stage
          AND TIMESTAMP(start_time) <= CURRENT_TIMESTAMP()
          AND TIMESTAMP(end_time) > CURRENT_TIMESTAMP()
        ORDER BY start_time DESC
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("stage", "STRING", stage),
        ])
        rows = list(client.query(query, job_config=job_config).result())
        return str(rows[0].dj) if rows else None
    except Exception as e:
        logging.error(f"Erreur get_now_playing_dj: {e}")
        return None


def get_first_set_start_utc(festival_id, day):
    """Heure de début (UTC, tz-aware) du TOUT PREMIER set d'un jour donné (nom de
    jour anglais minuscule, ex. 'friday'). None si aucun set/erreur. Sert à
    programmer le rappel « tente » 30 min avant le premier set."""
    try:
        query = f"""
        SELECT MIN(TIMESTAMP(start_time)) AS first_start
        FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id AND LOWER(day) = LOWER(@day)
          AND IFNULL(active, TRUE) = TRUE
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("day", "STRING", day),
        ])
        rows = list(client.query(query, job_config=job_config).result())
        if not rows or rows[0].first_start is None:
            return None
        return rows[0].first_start
    except Exception as e:
        logging.error(f"Erreur get_first_set_start_utc: {e}")
        return None


# ============================================================================
# ÉTAT PAR FESTIVAL (festival_users : géoloc + district courant par festival)
# ============================================================================

def get_bigquery_users(festival_id):
    """Récupère les utilisateurs présents sur un festival, avec leur position.
    Jointure users (global) × festival_users (état par festival)."""
    try:
        # LEFT JOIN : on renvoie TOUS les utilisateurs (comptes globaux) ; la
        # position (festival_users) est superposée si elle existe pour CE festival,
        # sinon NULL. Un INNER JOIN masquerait les users sans position et viderait
        # l'équipe d'un festival fraîchement ouvert.
        query = f"""
        SELECT
            u.id,
            u.username,
            u.phone_number,
            fu.last_lat,
            fu.last_lng,
            fu.last_location,
            fu.tent_lat,
            fu.tent_lng,
            u.user_role
        FROM `{Config.BQ_USERS}` u
        LEFT JOIN `{Config.BQ_FESTIVAL_USERS}` fu
          ON fu.user_id = u.id AND fu.festival_id = @festival_id
        ORDER BY u.username
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        df = client.query(query, job_config=job_config).result().to_dataframe()

        users = []
        for _, row in df.iterrows():
            users.append({
                "id": int(row["id"]) if not pd.isna(row["id"]) else None,
                "username": str(row["username"]) if not pd.isna(row["username"]) else None,
                "phone_number": str(row["phone_number"]) if not pd.isna(row["phone_number"]) else None,
                "last_lat": float(row["last_lat"]) if not pd.isna(row["last_lat"]) else None,
                "last_lng": float(row["last_lng"]) if not pd.isna(row["last_lng"]) else None,
                "last_location": str(row["last_location"]) if not pd.isna(row["last_location"]) else None,
                "tent_lat": float(row["tent_lat"]) if not pd.isna(row["tent_lat"]) else None,
                "tent_lng": float(row["tent_lng"]) if not pd.isna(row["tent_lng"]) else None,
                "user_role": str(row["user_role"]) if not pd.isna(row["user_role"]) else "user",
            })
        return users
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des utilisateurs: {e}")
        raise

def upsert_bigquery_festival_user(festival_id, user_id, lat, lng, stage):
    """Insère ou met à jour la ligne festival_users (position + scène courante)."""
    try:
        merge_query = f"""
        MERGE `{Config.BQ_FESTIVAL_USERS}` AS target
        USING (SELECT @festival_id AS festival_id, @user_id AS user_id) AS source
        ON target.festival_id = source.festival_id AND target.user_id = source.user_id
        WHEN MATCHED THEN
            UPDATE SET last_lat = @lat, last_lng = @lng, last_location = @stage
        WHEN NOT MATCHED THEN
            INSERT (festival_id, user_id, last_lat, last_lng, last_location)
            VALUES (@festival_id, @user_id, @lat, @lng, @stage)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("lat", "FLOAT64", lat),
                bigquery.ScalarQueryParameter("lng", "FLOAT64", lng),
                bigquery.ScalarQueryParameter("stage", "STRING", stage if stage else "?"),
            ]
        )
        client.query(merge_query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur upsert_bigquery_festival_user: {e}")
        raise

def update_bigquery_user_location(festival_id, user_id, lat, lng):
    """Met à jour uniquement les coordonnées d'un utilisateur sur un festival."""
    upsert_bigquery_festival_user(festival_id, user_id, lat, lng, None)

def update_bigquery_user_tent(festival_id, user_id, lat, lng):
    """Enregistre l'emplacement de la tente (campement) d'un utilisateur sur un
    festival. N'écrit QUE tent_lat/tent_lng (laisse la position courante
    intacte). MERGE → idempotent (insère la ligne festival_users si absente)."""
    try:
        merge_query = f"""
        MERGE `{Config.BQ_FESTIVAL_USERS}` AS target
        USING (SELECT @festival_id AS festival_id, @user_id AS user_id) AS source
        ON target.festival_id = source.festival_id AND target.user_id = source.user_id
        WHEN MATCHED THEN
            UPDATE SET tent_lat = @lat, tent_lng = @lng
        WHEN NOT MATCHED THEN
            INSERT (festival_id, user_id, tent_lat, tent_lng)
            VALUES (@festival_id, @user_id, @lat, @lng)
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            bigquery.ScalarQueryParameter("lat", "FLOAT64", lat),
            bigquery.ScalarQueryParameter("lng", "FLOAT64", lng),
        ])
        client.query(merge_query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur update_bigquery_user_tent: {e}")
        raise

def update_all_users_stage(festival_id):
    """Recalcule last_location (scène) pour TOUS les utilisateurs d'un festival
    à partir de leurs dernières coordonnées (appelé sur un événement 'perdu')."""
    try:
        query = f"""
        SELECT user_id, last_lat, last_lng
        FROM `{Config.BQ_FESTIVAL_USERS}`
        WHERE festival_id = @festival_id AND last_lat IS NOT NULL AND last_lng IS NOT NULL
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())

        for row in rows:
            stage = get_stage_from_coordinates(festival_id, row.last_lat, row.last_lng)
            update_query = f"""
            UPDATE `{Config.BQ_FESTIVAL_USERS}`
            SET last_location = @stage
            WHERE festival_id = @festival_id AND user_id = @user_id
            """
            update_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("stage", "STRING", stage if stage else "?"),
                    bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                    bigquery.ScalarQueryParameter("user_id", "INT64", row.user_id),
                ]
            )
            client.query(update_query, job_config=update_config).result()

        logging.info(f"Scène mise à jour pour tous les utilisateurs du festival {festival_id}.")
    except Exception as e:
        logging.error(f"Erreur update_all_users_stage: {e}")
        raise


# ============================================================================
# MÉTÉO
# ============================================================================

def store_bigquery_weather_forecast(festival_id, weather_data):
    """Stocke les prévisions météo d'un festival : on supprime d'abord les
    lignes existantes de CE festival (pas toute la table), puis on append."""
    try:
        # 1. Purge les anciennes prévisions de ce festival uniquement
        delete_query = f"DELETE FROM `{Config.BQ_PROJECT}.{Config.BQ_DATASET}.{Config.BQ_WEATHER_TABLE}` WHERE festival_id = @festival_id"
        delete_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        client.query(delete_query, job_config=delete_config).result()

        # 2. Append les nouvelles lignes (chacune taguée festival_id)
        rows = [{**row, "festival_id": festival_id} for row in weather_data]
        table_ref = client.dataset(Config.BQ_DATASET).table(Config.BQ_WEATHER_TABLE)
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )
        job = client.load_table_from_json(rows, table_ref, job_config=job_config)
        job.result()
    except Exception as e:
        logging.error(f"Erreur store_bigquery_weather_forecast: {e}")
        raise

def get_bigquery_weather_forecast(festival_id):
    """Récupère les prévisions météo d'un festival depuis BigQuery."""
    try:
        query = f"""
        SELECT
            date, day_name, temperature, description, icon, humidity, wind_speed, festival_day
        FROM `{Config.BQ_PROJECT}.{Config.BQ_DATASET}.{Config.BQ_WEATHER_TABLE}`
        WHERE festival_id = @festival_id
        ORDER BY date
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        df = client.query(query, job_config=job_config).result().to_dataframe()
        # Le nettoyage des NaN -> null est fait côté app.py (_nan_to_none), plus
        # robuste que df.where pour les colonnes entièrement NULL (float64).
        return df.to_dict('records')
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la météo: {e}")
        raise


# ============================================================================
# SCÈNES (table `stages`, ex-`districts`)
# ============================================================================

_STAGE_COLS = [
    "lat_avg", "lon_avg", "lat_avd", "lon_avd",
    "lat_arg", "lon_arg", "lat_ard", "lon_ard",
    "lat_rally_point", "lon_rally_point",
]

def _stage_row_to_dict(row):
    d = {"stage": str(row.stage)}
    for col in _STAGE_COLS:
        value = getattr(row, col)
        d[col] = float(value) if value is not None else None
    return d

def get_bigquery_stages(festival_id):
    """Récupère toutes les scènes d'un festival."""
    try:
        query = f"SELECT * FROM `{Config.BQ_STAGES}` WHERE festival_id = @festival_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = client.query(query, job_config=job_config).result()
        return [_stage_row_to_dict(row) for row in rows]
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des scènes: {e}")
        raise

def get_bigquery_stage(festival_id, stage_name):
    """Récupère une scène spécifique par son nom (au sein d'un festival)."""
    try:
        query = f"""
        SELECT * FROM `{Config.BQ_STAGES}`
        WHERE festival_id = @festival_id AND stage = @stage_name
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("stage_name", "STRING", stage_name),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return _stage_row_to_dict(rows[0]) if rows else None
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la scène {stage_name}: {e}")
        raise

def update_bigquery_stage(festival_id, stage_data):
    """Met à jour les coordonnées d'une scène."""
    try:
        query = f"""
        UPDATE `{Config.BQ_STAGES}`
        SET
            lat_avg = @lat_avg, lon_avg = @lon_avg,
            lat_avd = @lat_avd, lon_avd = @lon_avd,
            lat_arg = @lat_arg, lon_arg = @lon_arg,
            lat_ard = @lat_ard, lon_ard = @lon_ard,
            lat_rally_point = @lat_rally_point, lon_rally_point = @lon_rally_point
        WHERE festival_id = @festival_id AND stage = @stage
        """
        params = [
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("stage", "STRING", stage_data["stage"]),
        ]
        params += [bigquery.ScalarQueryParameter(col, "FLOAT64", stage_data[col]) for col in _STAGE_COLS]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        client.query(query, job_config=job_config).result()
        logging.info(f"Scène {stage_data['stage']} mise à jour (festival {festival_id}).")
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la scène {stage_data['stage']}: {e}")
        raise

def get_stage_from_coordinates(festival_id, lat, lng):
    """Retourne le nom de la scène si les coordonnées sont à l'intérieur, sinon None."""
    try:
        query = f"SELECT * FROM `{Config.BQ_STAGES}` WHERE festival_id = @festival_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = client.query(query, job_config=job_config).result()

        for row in rows:
            coords = [
                (row.lat_avg, row.lon_avg),
                (row.lat_avd, row.lon_avd),
                (row.lat_arg, row.lon_arg),
                (row.lat_ard, row.lon_ard),
            ]
            if any(c[0] is None or c[1] is None for c in coords):
                continue
            min_lat = min(c[0] for c in coords)
            max_lat = max(c[0] for c in coords)
            min_lon = min(c[1] for c in coords)
            max_lon = max(c[1] for c in coords)

            if min_lat <= lat <= max_lat and min_lon <= lng <= max_lon:
                return row.stage
        return None
    except Exception as e:
        logging.error(f"Erreur get_stage_from_coordinates: {e}")
        raise


# ============================================================================
# GÉOLOC
# ============================================================================

def insert_bigquery_geoloc(festival_id, user_id, lat, lng):
    """Insère une nouvelle entrée dans la table geoloc (festival_id, user_id, timestamp, lat, lon)."""
    try:
        table_ref = client.dataset(Config.BQ_DATASET).table('geoloc')
        row = {
            "festival_id": festival_id,
            "user_id": user_id,
            # Format RFC 3339 avec 'Z' : seul format garanti accepté par BQ en streaming insert.
            "timestamp": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z',
            "lat": lat,
            "lon": lng,
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            logging.error(f"Erreurs insert geoloc: {errors}")
            raise Exception(f"Erreurs BigQuery insert geoloc: {errors}")
        logging.info(f"Geoloc insérée pour user {user_id} (festival {festival_id}): lat={lat}, lon={lng}")
    except Exception as e:
        logging.error(f"Erreur insert_bigquery_geoloc: {e}")
        raise

def update_bigquery_user_location_and_stage(festival_id, user_id, lat, lng, stage):
    """Met à jour la position ET la scène courante d'un utilisateur (festival_users)."""
    upsert_bigquery_festival_user(festival_id, user_id, lat, lng, stage)


# ============================================================================
# ÉVÉNEMENTS (SOS / perdu / hype)
# ============================================================================

def insert_bigquery_event(festival_id, user_id, event_type):
    """Insère un nouvel événement via batch load (pas de streaming buffer).
    Les lignes batch sont immédiatement disponibles pour les DML (DELETE/UPDATE)."""
    try:
        table_ref = client.dataset(Config.BQ_DATASET).table('events')
        row = {
            "festival_id": festival_id,
            "user_id": user_id,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "event_type": event_type,
        }
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=[
                bigquery.SchemaField("festival_id", "INTEGER"),
                bigquery.SchemaField("user_id", "INTEGER"),
                bigquery.SchemaField("timestamp", "TIMESTAMP"),
                bigquery.SchemaField("event_type", "STRING"),
            ],
        )
        job = client.load_table_from_json([row], table_ref, job_config=job_config)
        job.result()
    except Exception as e:
        logging.error(f"Erreur insert_bigquery_event: {e}")
        raise

def delete_last_bigquery_event(festival_id, user_id):
    """Supprime le dernier événement (MAX timestamp) d'un utilisateur sur un festival."""
    try:
        query = f"""
        DELETE FROM `{Config.BQ_EVENTS}`
        WHERE festival_id = @festival_id AND user_id = @user_id
          AND timestamp = (
              SELECT MAX(timestamp)
              FROM `{Config.BQ_EVENTS}`
              WHERE festival_id = @festival_id AND user_id = @user_id
          )
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur delete_last_bigquery_event: {e}")
        raise

def get_bigquery_user_events(festival_id, user_id):
    """Récupère les événements d'un utilisateur sur un festival."""
    try:
        query = f"""
        SELECT user_id, timestamp, event_type
        FROM `{Config.BQ_EVENTS}`
        WHERE festival_id = @festival_id AND user_id = @user_id
        ORDER BY timestamp DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [
            {
                "user_id": int(row.user_id),
                "timestamp": row.timestamp.isoformat() if hasattr(row.timestamp, 'isoformat') else str(row.timestamp),
                "event_type": str(row.event_type),
            }
            for row in rows
        ]
    except Exception as e:
        logging.error(f"Erreur get_bigquery_user_events: {e}")
        raise


# ============================================================================
# PUSHS PROGRAMMÉS / JOURNAL
# ============================================================================
# Helpers servant à calculer les « gagnant·e·s » des notifications quotidiennes
# (cf. push_schedule.py) puis à journaliser/dédupliquer les envois.

def get_user_gender(user_id):
    """Genre d'un utilisateur : 'm' (il), 'f' (elle) ou None (inconnu).
    Best-effort → None si colonne absente / valeur vide / erreur."""
    try:
        query = f"SELECT gender FROM `{Config.BQ_USERS}` WHERE id = @user_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("user_id", "INT64", user_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return None
        g = rows[0].gender
        if g is None:
            return None
        g = str(g).strip().lower()
        return g if g in ("m", "f") else None
    except Exception as e:
        logging.error(f"Erreur get_user_gender: {e}")
        return None


def get_event_counts(festival_id, start_utc, end_utc):
    """Nombre d'events par (utilisateur, type) sur une fenêtre temporelle UTC.
    Les timestamps sont stockés en UTC → on passe des bornes UTC (datetime aware).
    Retourne [{"user_id", "event_type", "n"}, ...]."""
    try:
        query = f"""
        SELECT user_id, event_type, COUNT(*) AS n
        FROM `{Config.BQ_EVENTS}`
        WHERE festival_id = @festival_id
          AND timestamp >= @start_utc
          AND timestamp < @end_utc
        GROUP BY user_id, event_type
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("start_utc", "TIMESTAMP", start_utc),
            bigquery.ScalarQueryParameter("end_utc", "TIMESTAMP", end_utc),
        ])
        rows = client.query(query, job_config=job_config).result()
        return [
            {"user_id": int(r.user_id), "event_type": str(r.event_type), "n": int(r.n)}
            for r in rows
        ]
    except Exception as e:
        logging.error(f"Erreur get_event_counts: {e}")
        raise


def get_rating_count_by_user(festival_id, day=None):
    """Nombre de sets NOTÉS par utilisateur. Si `day` (ex. 'saturday') est fourni,
    on restreint aux sets programmés ce jour-là (même filtre que les Tendances).
    Retourne [{"user_id", "n"}, ...] trié décroissant."""
    return _count_favorites_by_user(festival_id, day, notation_only=True)


def get_favorite_count_by_user(festival_id, day=None):
    """Nombre de sets MIS EN FAVORI par utilisateur, optionnellement filtré au jour.
    Retourne [{"user_id", "n"}, ...] trié décroissant."""
    return _count_favorites_by_user(festival_id, day, notation_only=False)


def _count_favorites_by_user(festival_id, day, notation_only):
    try:
        condition = "uf.notation IS NOT NULL" if notation_only else "uf.isfavorite = TRUE"
        params = [bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        # On rattache TOUJOURS au set pour ne compter que les favoris sur des sets
        # ACTIFS (un set désactivé ne doit plus peser dans les palmarès), et pour
        # filtrer par jour si demandé.
        condition += " AND IFNULL(t.active, TRUE) = TRUE"
        if day is not None:
            condition += " AND LOWER(t.day) = @day"
            params.append(bigquery.ScalarQueryParameter("day", "STRING", day.lower()))
        query = f"""
        SELECT uf.user_id AS user_id, COUNT(*) AS n
        FROM `{Config.BQ_USER_FAVORITES}` uf
        JOIN `{Config.BQ_TIMETABLE}` t
          ON t.set_id = uf.set_id AND t.festival_id = @festival_id
        WHERE uf.festival_id = @festival_id AND {condition}
        GROUP BY uf.user_id
        ORDER BY n DESC
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = client.query(query, job_config=job_config).result()
        return [{"user_id": int(r.user_id), "n": int(r.n)} for r in rows]
    except Exception as e:
        logging.error(f"Erreur _count_favorites_by_user: {e}")
        raise


def get_user_top_fav_stage(festival_id, user_id, day=None):
    """Scène la plus représentée parmi les favoris d'un utilisateur (optionnellement
    pour un jour donné). Sert au gag « X traîne au bar de <scène> ». None si aucun
    favori."""
    try:
        params = [
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
        ]
        day_filter = ""
        if day is not None:
            day_filter = "AND LOWER(t.day) = @day"
            params.append(bigquery.ScalarQueryParameter("day", "STRING", day.lower()))
        query = f"""
        SELECT t.stage AS stage, COUNT(*) AS n
        FROM `{Config.BQ_USER_FAVORITES}` uf
        JOIN `{Config.BQ_TIMETABLE}` t
          ON t.set_id = uf.set_id AND t.festival_id = @festival_id
        WHERE uf.festival_id = @festival_id
          AND uf.user_id = @user_id
          AND uf.isfavorite = TRUE
          AND IFNULL(t.active, TRUE) = TRUE
          {day_filter}
        GROUP BY t.stage
        ORDER BY n DESC
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = list(client.query(query, job_config=job_config).result())
        return str(rows[0].stage) if rows and rows[0].stage is not None else None
    except Exception as e:
        logging.error(f"Erreur get_user_top_fav_stage: {e}")
        return None


def get_main_stage(festival_id):
    """Scène principale du festival = première dans le stage_order (repli alpha).
    Sert de repli quand un utilisateur n'a aucun favori."""
    try:
        query = f"""
        SELECT stage
        FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id AND stage IS NOT NULL
          AND IFNULL(active, TRUE) = TRUE
        GROUP BY stage, stage_order
        ORDER BY stage_order IS NULL, stage_order, LOWER(stage)
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return str(rows[0].stage) if rows else None
    except Exception as e:
        logging.error(f"Erreur get_main_stage: {e}")
        return None


def get_trending_djs(festival_id, day=None, limit=3):
    """DJs en tête du classement bayésien (même formule que le front : confidence
    C=3, prior = moyenne globale de toutes les notes du festival). Filtré au jour
    si `day` est fourni. Retourne une liste de noms de DJ (au plus `limit`)."""
    try:
        params = [
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("C", "FLOAT64", 3.0),
            bigquery.ScalarQueryParameter("lim", "INT64", limit),
        ]
        day_filter = ""
        if day is not None:
            day_filter = "AND LOWER(t.day) = @day"
            params.append(bigquery.ScalarQueryParameter("day", "STRING", day.lower()))
        query = f"""
        WITH ratings AS (
          SELECT set_id, notation
          FROM `{Config.BQ_USER_FAVORITES}`
          WHERE festival_id = @festival_id AND notation IS NOT NULL
        ),
        gm AS (SELECT AVG(notation) AS m FROM ratings),
        per_set AS (
          SELECT set_id, COUNT(*) AS n, SUM(notation) AS s, AVG(notation) AS avg
          FROM ratings GROUP BY set_id
        )
        SELECT t.dj AS dj,
               (@C * gm.m + ps.s) / (@C + ps.n) AS bayes,
               ps.n AS n, ps.avg AS avg
        FROM per_set ps
        JOIN `{Config.BQ_TIMETABLE}` t
          ON t.set_id = ps.set_id AND t.festival_id = @festival_id
        CROSS JOIN gm
        WHERE IFNULL(t.active, TRUE) = TRUE {day_filter}
        ORDER BY bayes DESC, n DESC, avg DESC, LOWER(t.dj)
        LIMIT @lim
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = client.query(query, job_config=job_config).result()
        return [str(r.dj) for r in rows if r.dj is not None]
    except Exception as e:
        logging.error(f"Erreur get_trending_djs: {e}")
        return []


def notification_exists(festival_id, slot_key):
    """True si une notification a déjà été générée pour ce créneau (anti-doublon)."""
    try:
        query = f"""
        SELECT COUNT(*) AS c
        FROM `{Config.BQ_NOTIFICATIONS}`
        WHERE festival_id = @festival_id AND slot_key = @slot_key
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("slot_key", "STRING", slot_key),
        ])
        rows = list(client.query(query, job_config=job_config).result())
        return rows[0].c > 0
    except Exception as e:
        logging.error(f"Erreur notification_exists: {e}")
        # En cas d'erreur, on renvoie True pour NE PAS risquer un envoi en double.
        return True


def insert_notification(festival_id, slot_key, day, scheduled_local, theme, title, body, pushed, variant=None):
    """Journalise une notification (DML INSERT → immédiatement requêtable pour le
    dédoublonnage, contrairement au streaming).
    `variant` identifie la formule d'un événement temps réel (dédup hype) ; NULL
    pour les pushs programmés."""
    try:
        query = f"""
        INSERT INTO `{Config.BQ_NOTIFICATIONS}`
          (festival_id, slot_key, day, scheduled_local, theme, title, body, pushed, variant)
        VALUES (@festival_id, @slot_key, @day, @scheduled_local, @theme, @title, @body, @pushed, @variant)
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ScalarQueryParameter("slot_key", "STRING", slot_key),
            bigquery.ScalarQueryParameter("day", "STRING", day),
            bigquery.ScalarQueryParameter("scheduled_local", "STRING", scheduled_local),
            bigquery.ScalarQueryParameter("theme", "STRING", theme),
            bigquery.ScalarQueryParameter("title", "STRING", title),
            bigquery.ScalarQueryParameter("body", "STRING", body),
            bigquery.ScalarQueryParameter("pushed", "BOOL", pushed),
            bigquery.ScalarQueryParameter("variant", "STRING", variant),
        ])
        client.query(query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur insert_notification: {e}")
        raise


def get_hype_variant_cycle_state(festival_id, pool_keys):
    """État du cycle de dédup 'hype' pour un palier : retourne
    (count, recent_keys) où `count` est le nombre total de variantes de ce palier
    déjà journalisées pour le festival, et `recent_keys` les plus récentes d'abord
    (limitées à la taille du palier). L'appelant en déduit la position dans le
    cycle (`count % P`) et les clés déjà utilisées dans le cycle courant.
    Best-effort → (0, []) si rien/erreur (choix aléatoire ; l'envoi ne doit jamais
    échouer ici)."""
    try:
        if not pool_keys:
            return 0, []
        query = f"""
        SELECT variant, COUNT(*) OVER() AS total
        FROM `{Config.BQ_NOTIFICATIONS}`
        WHERE festival_id = @festival_id
          AND theme = 'hype'
          AND variant IN UNNEST(@pool_keys)
        ORDER BY created_at DESC
        LIMIT @limit
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id),
            bigquery.ArrayQueryParameter("pool_keys", "STRING", list(pool_keys)),
            bigquery.ScalarQueryParameter("limit", "INT64", len(pool_keys)),
        ])
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return 0, []
        count = int(rows[0].total)
        recent_keys = [str(r.variant) for r in rows]
        return count, recent_keys
    except Exception as e:
        logging.error(f"Erreur get_hype_variant_cycle_state: {e}")
        return 0, []


def get_journal(festival_id):
    """Toutes les notifications d'un festival, les plus récentes d'abord (page Journal)."""
    try:
        query = f"""
        SELECT slot_key, day, scheduled_local, theme, title, body, pushed, created_at
        FROM `{Config.BQ_NOTIFICATIONS}`
        WHERE festival_id = @festival_id
        ORDER BY created_at DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        rows = client.query(query, job_config=job_config).result()
        return [
            {
                "slot_key": str(r.slot_key),
                "day": str(r.day) if r.day is not None else None,
                "scheduled_local": str(r.scheduled_local) if r.scheduled_local is not None else None,
                "theme": str(r.theme) if r.theme is not None else None,
                "title": str(r.title),
                "body": str(r.body),
                "pushed": bool(r.pushed) if r.pushed is not None else False,
                "created_at": r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at),
            }
            for r in rows
        ]
    except Exception as e:
        logging.error(f"Erreur get_journal: {e}")
        raise
