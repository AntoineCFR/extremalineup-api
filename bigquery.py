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
    """Récupère UNIQUEMENT les set_id favoris d'un utilisateur (pour le frontend)."""
    try:
        query = f"""
        SELECT set_id
        FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = '{user_id}'
        """
        df = client.query(query).result().to_dataframe()
        return df['set_id'].tolist()  # Retourne une liste de set_id (ex: [28, 45, 67])
    except Exception as e:
        logging.error(f"Erreur lors de la récupération des set_id favoris: {e}")
        raise

def update_user_favorites(user_id, favorites_list):
    """Met à jour TOUS les favoris d'un utilisateur (remplace la liste existante)."""
    try:
        # 1. Supprime les anciens favoris de l'utilisateur
        delete_query = f"""
        DELETE FROM `{Config.BQ_USER_FAVORITES}`
        WHERE user_id = '{user_id}'
        """
        client.query(delete_query).result()

        # 2. Ajoute les nouveaux favoris (si la liste n'est pas vide)
        if favorites_list:
            # Utilise une requête paramétrée pour éviter les injections SQL
            # et insère tous les favoris en une seule requête
            values = ", ".join([f"('{user_id}', {set_id})" for set_id in favorites_list])
            insert_query = f"""
            INSERT INTO `{Config.BQ_USER_FAVORITES}`
            (user_id, set_id)
            VALUES {values}
            """
            client.query(insert_query).result()
    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour des favoris: {e}")
        raise

def user_exists(user_id):
    """Vérifie si un utilisateur existe dans la table users."""
    try:
        query = f"""
        SELECT COUNT(*) as count
        FROM `{Config.BQ_DATASET}.users`
        WHERE username = '{user_id}'
        """
        df = client.query(query).result().to_dataframe()
        return df.iloc[0]['count'] > 0
    except Exception as e:
        logging.error(f"Erreur lors de la vérification de l'utilisateur: {e}")
        raise