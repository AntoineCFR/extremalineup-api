import logging
from google.cloud import bigquery
from google.oauth2 import service_account
import pandas
from config import Config

def get_google_credentials(credentials_path=None):
    credentials_path = credentials_path or Config.GOOGLE_APPLICATION_CREDENTIALS
    return service_account.Credentials.from_service_account_file(credentials_path)

# Initialise le client BigQuery avec les credentials
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

def get_user_favorites(user_id):
    """Récupère UNIQUEMENT les set_id favoris d'un utilisateur via son user_id (INT64)."""
    try:
        query = f"""
        SELECT set_id
        FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = {user_id}  # user_id est un INT64, pas besoin de quotes
        """
        df = client.query(query).result().to_dataframe()
        return df['set_id'].tolist()  # Retourne une liste de set_id (ex: [28, 45, 67])
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des set_id favoris: {e}")
        raise

def update_user_favorites(user_id, favorites_list):
    """Met à jour TOUS les favoris d'un utilisateur via son user_id (INT64)."""
    try:
        # 1. Supprime les anciens favoris de l'utilisateur
        delete_query = f"""
        DELETE FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = {user_id}  # user_id est un INT64
        """
        client.query(delete_query).result()

        # 2. Ajoute les nouveaux favoris (si la liste n'est pas vide)
        if favorites_list:
            # Construis les valeurs sans quotes pour user_id (INT64)
            values = ", ".join([f"({user_id}, {set_id})" for set_id in favorites_list])
            insert_query = f"""
            INSERT INTO `{Config.BQ_USER_FAVORITES}`
            (user_id, set_id)
            VALUES {values}
            """
            client.query(insert_query).result()
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour des favoris: {e}")
        raise

def get_user_id(username):
    """Récupère l'ID d'un utilisateur depuis son username (STRING). Retourne None si introuvable."""
    try:
        query = f"""
        SELECT id
        FROM `{Config.BQ_DATASET}.users`
        WHERE username = '{username.replace("'", "''")}'  # Échappe les quotes pour éviter les injections SQL
        """
        df = client.query(query).result().to_dataframe()
        return df.iloc[0]['id'] if not df.empty else None
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de l'user_id: {e}")
        raise

def user_exists(username):
    """Vérifie si un utilisateur existe dans la table users (via son username)."""
    try:
        query = f"""
        SELECT COUNT(*) as count
        FROM `{Config.BQ_DATASET}.users`
        WHERE username = '{username.replace("'", "''")}'  # Échappe les quotes
        """
        df = client.query(query).result().to_dataframe()
        return df.iloc[0]['count'] > 0
    except Exception as e:
        logging.error(f"Erreur lors de la vérification de l'utilisateur: {e}")
        raise

def store_weather_forecast(weather_data):
    """
    Stocke les prévisions météo pour les 3 jours du festival dans BigQuery.
    Args:
        weather_data (list): Liste de dictionnaires avec les données météo pour chaque jour.
                             Format attendu: [
                                 {
                                     "date": "2026-05-15",
                                     "day_name": "Vendredi",
                                     "temperature": 18.5,
                                     "description": "Partiellement nuageux",
                                     "icon": "https://.../116.png",
                                     "humidity": 65,
                                     "wind_speed": 4.2,
                                     "festival_day": "friday"
                                 },
                                 ...
                             ]
    """
    try:
        if not weather_data:
            logging.warning("Aucune donnée météo fournie.")
            return

        # Supprime les anciennes données pour éviter les doublons
        delete_query = f"""
        DELETE FROM `{Config.BQ_DATASET}.{Config.BQ_WEATHER_TABLE}`
        WHERE festival_day IN ('friday', 'saturday', 'sunday')
        """
        client.query(delete_query).result()

        # Insère les nouvelles données
        table_ref = client.dataset(Config.BQ_DATASET).table(Config.BQ_WEATHER_TABLE)
        errors = client.insert_rows_json(table_ref, weather_data)
        if errors:
            logging.error(f"Erreurs lors de l'insertion dans BigQuery: {errors}")
            raise Exception(f"Erreurs BigQuery: {errors}")

    except Exception as e:
        logging.error(f"Erreur lors du stockage de la météo: {e}")
        raise

def get_weather_forecast():
    """
    Récupère les prévisions météo pour les 3 jours du festival depuis BigQuery.
    Returns:
        list: Liste de dictionnaires avec les données météo pour chaque jour.
    """
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
            `{Config.BQ_DATASET}.{Config.BQ_WEATHER_TABLE}`
        WHERE
            festival_day IN ('friday', 'saturday', 'sunday')
        ORDER BY
            date
        """
        df = client.query(query).result().to_dataframe()
        return df.to_dict('records')  # Retourne une liste de dicts
    except Exception as e:
        logging.error(f"Erreur lors de la récupération de la météo: {e}")
        raise

def get_users():
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
            `{Config.BQ_DATASET}.users`
        ORDER BY
            username
        """
        df = client.query(query).result().to_dataframe()
        # Convertis en liste de dictionnaires (format JSON-friendly)
        users = df.to_dict('records')
        # Convertis les valeurs BigQuery (ex: numpy.float64) en types natifs Python
        for user in users:
            if pandas.notna(user.get('last_lat')):
                user['last_lat'] = float(user['last_lat'])
            if pandas.notna(user.get('last_lng')):
                user['last_lng'] = float(user['last_lng'])
        return users
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des utilisateurs: {e}")
        raise