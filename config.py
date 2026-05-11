import os

class Config:
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(credentials_json)
        GOOGLE_APPLICATION_CREDENTIALS = f.name
    
    # BigQuery
    BQ_PROJECT = "extremalineup"
    BQ_DATASET = "dataset"
    BQ_TIMETABLE = f"{BQ_PROJECT}.{BQ_DATASET}.timetable"
    BQ_USER_FAVORITES = f"{BQ_PROJECT}.{BQ_DATASET}.user_favorites"

    # Flask
    FLASK_PORT = 5000
    FLASK_HOST = "0.0.0.0"