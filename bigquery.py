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

def get_bigquery_timetable():
    """Récupère la table timetable depuis BigQuery sous forme de DataFrame."""
    try:
        query = f"SELECT * FROM `{Config.BQ_TIMETABLE}` ORDER BY day_int, district, start_time"
        return client.query(query).result().to_dataframe()
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la timetable: {e}")
        raise

def get_bigquery_user_favorites(user_id=None):
    """
    Récupère les favoris depuis BigQuery.
    - Si user_id est fourni : retourne [{"set_id": 1, "isfavorite": true, "notation": 5}, ...] pour CET utilisateur
    - Si user_id est None : retourne [{"user_id": 1, "set_id": 42, "isfavorite": true, "notation": 5}, ...] pour TOUS les utilisateurs
    """
    try:
        if user_id is not None:
            # Mode "un seul utilisateur"
            query = f"""
            SELECT set_id, isfavorite, notation
            FROM `{Config.BQ_USER_FAVORITES}`
            WHERE user_id = {user_id}
            ORDER BY set_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("user_id", "INT64", user_id)]
            )
            rows = client.query(query, job_config=job_config).result()

            favorites = []
            for row in rows:
                favorites.append({
                    "set_id": int(row.set_id),
                    "isfavorite": bool(row.isfavorite),
                    "notation": int(row.notation) if row.notation is not None else None
                })
            return favorites
        else:
            # Mode "TOUS les utilisateurs"
            query = f"""
            SELECT user_id, set_id, isfavorite, notation
            FROM `{Config.BQ_USER_FAVORITES}`
            ORDER BY user_id, set_id
            """
            rows = client.query(query).result()

            favorites = []
            for row in rows:
                favorites.append({
                    "user_id": int(row.user_id),
                    "set_id": int(row.set_id),
                    "isfavorite": bool(row.isfavorite),
                    "notation": int(row.notation) if row.notation is not None else None
                })
            return favorites
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des favoris: {e}")
        raise

def toggle_bigquery_user_favorite(user_id, set_id):
    """
    Toggle isfavorite pour un couple (user_id, set_id).
    Retourne la nouvelle valeur de isfavorite.
    """
    try:
        query = f"""
        SELECT isfavorite
        FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = @user_id AND set_id = @set_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        current_value = rows[0].isfavorite if rows else False
        new_value = not current_value

        update_query = f"""
        UPDATE `{Config.BQ_USER_FAVORITES}`
        SET isfavorite = @new_value
        WHERE user_id = @user_id AND set_id = @set_id
        """
        update_job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("new_value", "BOOL", new_value),
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("set_id", "INT64", set_id)
            ]
        )
        client.query(update_query, job_config=update_job_config).result()
        return new_value
    except Exception as e:
        logging.error(f"Erreur lors du toggle favori: {e}")
        raise

def update_bigquery_user_favorite_notation(user_id, set_id, notation):
    """
    Met à jour la notation pour un couple (user_id, set_id).
    notation peut être None pour supprimer la note (→ SQL NULL).
    """
    try:
        if notation is None:
            # NULL ne peut pas être passé comme paramètre typé INT64 ; on l'écrit littéralement
            query = f"""
            UPDATE `{Config.BQ_USER_FAVORITES}`
            SET notation = NULL
            WHERE user_id = @user_id AND set_id = @set_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                    bigquery.ScalarQueryParameter("set_id", "INT64", set_id)
                ]
            )
        else:
            query = f"""
            UPDATE `{Config.BQ_USER_FAVORITES}`
            SET notation = @notation
            WHERE user_id = @user_id AND set_id = @set_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("notation", "INT64", notation),
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                    bigquery.ScalarQueryParameter("set_id", "INT64", set_id)
                ]
            )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la notation: {e}")
        raise

# --- Fonctions inchangées ---
def get_bigquery_user_id(username):
    """Récupère l'ID d'un utilisateur depuis son username (STRING). Retourne None si introuvable."""
    try:
        query = f"""
        SELECT id
        FROM `{Config.BQ_USERS}`
        WHERE username = '{username.replace("'", "''")}'
        """
        df = client.query(query).result().to_dataframe()
        return df.iloc[0]['id'] if not df.empty else None
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de l'user_id: {e}")
        raise

def bigquery_user_exists(username):
    """Vérifie si un utilisateur existe dans la table users (via son username)."""
    try:
        query = f"""
        SELECT COUNT(*) as count
        FROM `{Config.BQ_USERS}`
        WHERE username = '{username.replace("'", "''")}'
        """
        df = client.query(query).result().to_dataframe()
        return df.iloc[0]['count'] > 0
    except Exception as e:
        logging.error(f"Erreur lors de la vérification de l'utilisateur: {e}")
        raise

def store_bigquery_weather_forecast(weather_data):
    
    # Référence à la table de destination
    table_ref = client.dataset(Config.BQ_DATASET).table(Config.BQ_WEATHER_TABLE)

    # Configuration pour écraser la table existante
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",  # Écrase la table
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
    )

    # Charge les données dans la table (écrase l'ancienne)
    job = client.load_table_from_json(
        weather_data,
        table_ref,
        job_config=job_config
    )

    # Attend la fin du job
    job.result()

def get_bigquery_weather_forecast():
    """Récupère les prévisions météo pour les 3 jours du festival depuis BigQuery."""
    try:
        query = f"""
        SELECT
            date,
            day_name,
            temperature,
            description,
            icon,
            humidity,
            wind_speed,
            festival_day
        FROM
            `{Config.BQ_PROJECT}.{Config.BQ_DATASET}.{Config.BQ_WEATHER_TABLE}`
        WHERE
            festival_day IN ('friday', 'saturday', 'sunday')
        ORDER BY
            date
        """
        df = client.query(query).result().to_dataframe()
        return df.to_dict('records')
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la météo: {e}")
        raise

def get_bigquery_users():
    """Récupère la liste de tous les utilisateurs avec username, phone_number, last_lat, last_lng."""
    try:
        query = f"""
        SELECT
            id,
            username,
            phone_number,
            last_lat,
            last_lng
        FROM
            `{Config.BQ_USERS}`
        ORDER BY
            username
        """
        df = client.query(query).result().to_dataframe()

        users = []
        for _, row in df.iterrows():
            user = {
                "id": int(row["id"]) if not pd.isna(row["id"]) else None,
                "username": str(row["username"]) if not pd.isna(row["username"]) else None,
                "phone_number": str(row["phone_number"]) if not pd.isna(row["phone_number"]) else None,
                "last_lat": float(row["last_lat"]) if not pd.isna(row["last_lat"]) else None,
                "last_lng": float(row["last_lng"]) if not pd.isna(row["last_lng"]) else None,
            }
            users.append(user)

        return users
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des utilisateurs: {e}")
        raise

def update_bigquery_user_phone(user_id, phone_number):
    """Met à jour le numéro de téléphone d'un utilisateur dans BigQuery."""
    try:
        escaped_phone = phone_number.replace("'", "''")
        query = f"""
        UPDATE `{Config.BQ_USERS}`
        SET phone_number = '{escaped_phone}'
        WHERE id = {user_id}
        """
        client.query(query).result()
        logging.info(f"Numéro de téléphone mis à jour pour l'utilisateur {user_id}.")
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour du numéro de téléphone: {e}")
        raise

def update_bigquery_user_location(user_id, lat, lng):
    """Met à jour les coordonnées de localisation d'un utilisateur dans BigQuery."""
    try:
        query = f"""
        UPDATE `{Config.BQ_USERS}`
        SET last_lat = {lat}, last_lng = {lng}
        WHERE id = {user_id}
        """
        client.query(query).result()
        logging.info(f"Localisation mise à jour pour l'utilisateur {user_id}.")
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la localisation: {e}")
        raise


def get_bigquery_districts():
    """Récupère tous les districts depuis BigQuery."""
    try:
        query = f"SELECT * FROM `{Config.BQ_DISTRICTS}`"
        rows = client.query(query).result()
        districts = []
        for row in rows:
            districts.append({
                "district": str(row.district),
                "lat_avg": float(row.lat_avg) if row.lat_avg is not None else None,
                "lon_avg": float(row.lon_avg) if row.lon_avg is not None else None,
                "lat_avd": float(row.lat_avd) if row.lat_avd is not None else None,
                "lon_avd": float(row.lon_avd) if row.lon_avd is not None else None,
                "lat_arg": float(row.lat_arg) if row.lat_arg is not None else None,
                "lon_arg": float(row.lon_arg) if row.lon_arg is not None else None,
                "lat_ard": float(row.lat_ard) if row.lat_ard is not None else None,
                "lon_ard": float(row.lon_ard) if row.lon_ard is not None else None,
                "lat_rally_point": float(row.lat_rally_point) if row.lat_rally_point is not None else None,
                "lon_rally_point": float(row.lon_rally_point) if row.lon_rally_point is not None else None,
            })
        return districts
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des districts: {e}")
        raise

def get_bigquery_district(district_name):
    """Récupère un district spécifique par son nom."""
    try:
        query = f"""
        SELECT * FROM `{Config.BQ_DISTRICTS}`
        WHERE district = @district_name
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("district_name", "STRING", district_name),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        if not rows:
            return None
        row = rows[0]
        return {
            "district": str(row.district),
            "lat_avg": float(row.lat_avg) if row.lat_avg is not None else None,
            "lon_avg": float(row.lon_avg) if row.lon_avg is not None else None,
            "lat_avd": float(row.lat_avd) if row.lat_avd is not None else None,
            "lon_avd": float(row.lon_avd) if row.lon_avd is not None else None,
            "lat_arg": float(row.lat_arg) if row.lat_arg is not None else None,
            "lon_arg": float(row.lon_arg) if row.lon_arg is not None else None,
            "lat_ard": float(row.lat_ard) if row.lat_ard is not None else None,
            "lon_ard": float(row.lon_ard) if row.lon_ard is not None else None,
            "lat_rally_point": float(row.lat_rally_point) if row.lat_rally_point is not None else None,
            "lon_rally_point": float(row.lon_rally_point) if row.lon_rally_point is not None else None,
        }
    except Exception as e:
        logging.error(f"Erreur lors de la récupération du district {district_name}: {e}")
        raise

def update_bigquery_district(district_data):
    """Met à jour les coordonnées d'un district."""
    try:
        query = f"""
        UPDATE `{Config.BQ_DISTRICTS}`
        SET
            lat_avg = @lat_avg,
            lon_avg = @lon_avg,
            lat_avd = @lat_avd,
            lon_avd = @lon_avd,
            lat_arg = @lat_arg,
            lon_arg = @lon_arg,
            lat_ard = @lat_ard,
            lon_ard = @lon_ard,
            lat_rally_point = @lat_rally_point,
            lon_rally_point = @lon_rally_point
        WHERE district = @district
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("district", "STRING", district_data["district"]),
                bigquery.ScalarQueryParameter("lat_avg", "FLOAT64", district_data["lat_avg"]),
                bigquery.ScalarQueryParameter("lon_avg", "FLOAT64", district_data["lon_avg"]),
                bigquery.ScalarQueryParameter("lat_avd", "FLOAT64", district_data["lat_avd"]),
                bigquery.ScalarQueryParameter("lon_avd", "FLOAT64", district_data["lon_avd"]),
                bigquery.ScalarQueryParameter("lat_arg", "FLOAT64", district_data["lat_arg"]),
                bigquery.ScalarQueryParameter("lon_arg", "FLOAT64", district_data["lon_arg"]),
                bigquery.ScalarQueryParameter("lat_ard", "FLOAT64", district_data["lat_ard"]),
                bigquery.ScalarQueryParameter("lon_ard", "FLOAT64", district_data["lon_ard"]),
                bigquery.ScalarQueryParameter("lat_rally_point", "FLOAT64", district_data["lat_rally_point"]),
                bigquery.ScalarQueryParameter("lon_rally_point", "FLOAT64", district_data["lon_rally_point"]),
            ]
        )
        client.query(query, job_config=job_config).result()
        logging.info(f"District {district_data['district']} mis à jour.")
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour du district {district_data['district']}: {e}")
        raise

def get_district_from_coordinates(lat, lng):
    """Retourne le nom du district si les coordonnées sont à l'intérieur, sinon None."""
    try:
        query = f"SELECT * FROM `{Config.BQ_DISTRICTS}`"
        rows = client.query(query).result()

        for row in rows:
            # Calcule le rectangle englobant du district
            coords = [
                (row.lat_avg, row.lon_avg),
                (row.lat_avd, row.lon_avd),
                (row.lat_arg, row.lon_arg),
                (row.lat_ard, row.lon_ard),
            ]
            min_lat = min(c[0] for c in coords)
            max_lat = max(c[0] for c in coords)
            min_lon = min(c[1] for c in coords)
            max_lon = max(c[1] for c in coords)

            # Vérifie si le point est dans le rectangle
            if min_lat <= lat <= max_lat and min_lon <= lng <= max_lon:
                return row.district
        return None
    except Exception as e:
        logging.error(f"Erreur get_district_from_coordinates: {e}")
        raise

def insert_bigquery_geoloc(user_id, lat, lng, district=None):
    """Insère une nouvelle entrée dans la table geoloc."""
    try:
        table_ref = client.dataset(Config.BQ_DATASET).table('geoloc')
        row = {
            "user_id": user_id,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "lat": lat,
            "lon": lng,
            "district": district,
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            logging.error(f"Erreurs insert geoloc: {errors}")
            raise Exception(f"Erreurs BigQuery: {errors}")
    except Exception as e:
        logging.error(f"Erreur insert_bigquery_geoloc: {e}")
        raise

def update_bigquery_user_location_and_district(user_id, lat, lng, district):
    """Met à jour la localisation ET le district d'un utilisateur."""
    try:
        query = f"""
        UPDATE `{Config.BQ_USERS}`
        SET
            last_lat = @lat,
            last_lng = @lng,
            last_location = @district
        WHERE id = @user_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                bigquery.ScalarQueryParameter("lat", "FLOAT64", lat),
                bigquery.ScalarQueryParameter("lng", "FLOAT64", lng),
                bigquery.ScalarQueryParameter("district", "STRING", district if district else "?"),
            ]
        )
        client.query(query, job_config=job_config).result()
    except Exception as e:
        logging.error(f"Erreur update_bigquery_user_location_and_district: {e}")
        raise

def insert_bigquery_event(user_id, event_type):
    """Insère un nouvel événement."""
    try:
        table_ref = client.dataset(Config.BQ_DATASET).table('events')
        row = {
            "user_id": user_id,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "event_type": event_type,
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            logging.error(f"Erreurs insert event: {errors}")
            raise Exception(f"Erreurs BigQuery: {errors}")
    except Exception as e:
        logging.error(f"Erreur insert_bigquery_event: {e}")
        raise

def update_all_users_district():
    """Met à jour last_location pour TOUS les utilisateurs (appelé quand quelqu'un se déclare perdu)."""
    try:
        # Récupère tous les utilisateurs avec une localisation
        query = f"""
        SELECT id, last_lat, last_lng FROM `{Config.BQ_USERS}`
        WHERE last_lat IS NOT NULL AND last_lng IS NOT NULL
        """
        rows = client.query(query).result()

        for row in rows:
            user_id = row.id
            district = get_district_from_coordinates(row.last_lat, row.last_lng)

            update_query = f"""
            UPDATE `{Config.BQ_USERS}`
            SET last_location = @district
            WHERE id = @user_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("district", "STRING", district if district else "?"),
                    bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
                ]
            )
            client.query(update_query, job_config=job_config).result()

        logging.info("District mis à jour pour tous les utilisateurs.")
    except Exception as e:
        logging.error(f"Erreur update_all_users_district: {e}")
        raise

def get_bigquery_user_events(user_id):
    """Récupère les événements d'un utilisateur depuis BigQuery."""
    try:
        query = f"""
        SELECT user_id, timestamp, event_type
        FROM `{Config.BQ_EVENTS}`
        WHERE user_id = @user_id
        ORDER BY timestamp DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [
            {
                "user_id": int(row.user_id),
                "timestamp": row.timestamp.isoformat() if hasattr(row.timestamp, 'isoformat') else str(row.timestamp),
                "event_type": str(row.event_type)
            }
            for row in rows
        ]
    except Exception as e:
        logging.error(f"Erreur get_bigquery_user_events: {e}")
        raise

def get_username_by_id(user_id):
    """Récupère le username d'un utilisateur."""
    try:
        query = f"SELECT username FROM `{Config.BQ_USERS}` WHERE id = @user_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return rows[0].username if rows else "Utilisateur inconnu"
    except Exception as e:
        logging.error(f"Erreur get_username_by_id: {str(e)}")
        return "Utilisateur inconnu"