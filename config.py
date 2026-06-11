import os

class Config:
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not credentials_json:
        raise EnvironmentError(
            "La variable d'environnement GOOGLE_APPLICATION_CREDENTIALS_JSON n'est pas définie. "
            "Vérifiez la configuration de votre déploiement (Render → Environment Variables)."
        )
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(credentials_json)
        GOOGLE_APPLICATION_CREDENTIALS = f.name

    # Clé API WeatherAPI
    WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

    # Configuration BigQuery pour la météo
    BQ_WEATHER_TABLE = "weather"  # Nom de la table BigQuery

    # NB : la ville, les dates et le fuseau horaire ne sont plus codés en dur.
    # Ils proviennent désormais de la table `festivals` (voir get_festival()).

    # BigQuery
    BQ_PROJECT = "extremalineup"
    BQ_DATASET = "dataset"
    BQ_FESTIVALS = f"{BQ_PROJECT}.{BQ_DATASET}.festivals"
    BQ_FESTIVAL_USERS = f"{BQ_PROJECT}.{BQ_DATASET}.festival_users"
    BQ_TIMETABLE = f"{BQ_PROJECT}.{BQ_DATASET}.timetable"
    BQ_USER_FAVORITES = f"{BQ_PROJECT}.{BQ_DATASET}.user_favorites"
    BQ_USERS = f"{BQ_PROJECT}.{BQ_DATASET}.users"
    BQ_STAGES = f"{BQ_PROJECT}.{BQ_DATASET}.stages"  # ex-`districts`
    BQ_GEOLOC = f"{BQ_PROJECT}.{BQ_DATASET}.geoloc"
    BQ_EVENTS = f"{BQ_PROJECT}.{BQ_DATASET}.events"

    # Flask
    FLASK_PORT = 5000
    FLASK_HOST = "0.0.0.0"