import logging
import datetime
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
        query = f"""
        SELECT * FROM `{Config.BQ_TIMETABLE}`
        WHERE festival_id = @festival_id
        ORDER BY day_int, stage, start_time
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("festival_id", "INT64", festival_id)]
        )
        return client.query(query, job_config=job_config).result().to_dataframe()
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la timetable: {e}")
        raise


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
