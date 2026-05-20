import os

class Config:
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(credentials_json)
        GOOGLE_APPLICATION_CREDENTIALS = f.name
    
    # Clé API WeatherAPI
    WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

    # Emplacement du festival
    CITY = 'Houthalen-Helchteren'

    # Configuration BigQuery pour la météo
    BQ_WEATHER_TABLE = "weather"  # Nom de la table BigQuery

    # Dictionnaire pour mapper les dates aux jours du festival
    FESTIVAL_DAYS = {
        "2026-05-22": "friday",
        "2026-05-23": "saturday",
        "2026-05-24": "sunday",
    }


    # BigQuery
    BQ_PROJECT = "extremalineup"
    BQ_DATASET = "dataset"
    BQ_TIMETABLE = f"{BQ_PROJECT}.{BQ_DATASET}.timetable"
    BQ_USER_FAVORITES = f"{BQ_PROJECT}.{BQ_DATASET}.user_favorites"
    BQ_USERS = f"{BQ_PROJECT}.{BQ_DATASET}.users"
    BQ_DISTRICTS = f"{BQ_PROJECT}.{BQ_DATASET}.districts"
    BQ_GEOLOC = f"{BQ_PROJECT}.{BQ_DATASET}.geoloc"

    # Flask
    FLASK_PORT = 5000
    FLASK_HOST = "0.0.0.0"