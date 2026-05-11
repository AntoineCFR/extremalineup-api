import logging
from google.cloud import bigquery
from google.oauth2 import service_account
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
    """Récupère les favoris d'un utilisateur avec les détails du set."""
    try:
        query = f"""
        SELECT t.*
        FROM `{Config.BQ_USER_FAVORITES}` f
        JOIN `{Config.BQ_TIMETABLE}` t ON f.set_id = t.set_id
        WHERE f.user_id = '{user_id}'
        ORDER BY t.start_time
        """
        return client.query(query).result().to_dataframe()
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des favoris: {e}")
        raise

def toggle_favorite(user_id, set_id):
    """Ajoute ou retire un favori pour un utilisateur."""
    try:
        # Vérifie que le set_id existe dans timetable
        set_exists = client.query(f"""
            SELECT COUNT(*) AS count
            FROM `{Config.BQ_TIMETABLE}`
            WHERE set_id = {set_id}
        """).result().to_dataframe().iloc[0]['count'] > 0

        if not set_exists:
            raise ValueError(f"set_id {set_id} introuvable dans timetable")

        # Vérifie si le favori existe déjà
        query = f"""
        SELECT COUNT(*) AS count
        FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = '{user_id}' AND set_id = {set_id}
        """
        count = client.query(query).result().to_dataframe().iloc[0]['count']

        if count > 0:
            # Retire le favori
            query = f"""
            DELETE FROM `{Config.BQ_USER_FAVORITES}`
            WHERE user_id = '{user_id}' AND set_id = {set_id}
            """
        else:
            # Ajoute le favori
            query = f"""
            INSERT INTO `{Config.BQ_USER_FAVORITES}`
            (user_id, set_id)
            VALUES ('{user_id}', {set_id})
            """
        client.query(query).result()
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour des favoris: {e}")
        raise