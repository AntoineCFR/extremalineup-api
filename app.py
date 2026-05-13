from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
from datetime import datetime, timedelta
import requests
import logging
from bigquery import (
    get_bigquery_timetable,
    get_user_favorites,
    update_user_favorites,
    get_user_id,
    store_weather_forecast,
    get_weather_forecast
)
from config import Config

# Configure les logs pour le débogage
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# --- Endpoints existants (inchangés) ---
@app.route('/timetable', methods=['GET'])
def get_timetable():
    try:
        df = get_bigquery_timetable()
        df['start_time'] += pd.Timedelta(hours=2)
        df['end_time'] += pd.Timedelta(hours=2)
        return jsonify(df.to_dict(orient='records'))
    except Exception as e:
        logger.error(f"Erreur dans /timetable: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/favorites', methods=['GET'])
def get_favorites():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        user_id_int = int(user_id)
        favorite_set_ids = get_user_favorites(user_id_int)
        return jsonify({"favorites": [{"set_id": int(sid)} for sid in favorite_set_ids]})
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /favorites (GET): {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/favorites', methods=['POST'])
def save_favorites():
    data = request.get_json()
    user_id = data.get('user_id')
    favorites_list = data.get('favorites', [])

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        update_user_favorites(int(user_id), favorites_list)
        return jsonify({"status": "success"})
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /favorites (POST): {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/check', methods=['GET'])
def check_user():
    username = request.args.get('username')
    if not username:
        return jsonify({"error": "username is required"}), 400

    try:
        user_id = get_user_id(username)
        if user_id is None:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "user_id": int(user_id)})
    except Exception as e:
        logger.error(f"Erreur dans /users/check: {str(e)}")
        return jsonify({"error": str(e)}), 500

# --- NOUVEAUX ENDPOINTS POUR LA MÉTÉO ---
@app.route('/update-weather', methods=['POST'])
def update_weather():
    """
    Met à jour les prévisions météo pour les 3 jours du festival (22-24 mai 2026).
    Utilise uniquement forecast.json de WeatherAPI.
    """
    try:
        # Vérifie si on est après le festival (24 mai 2026)
        if datetime.now().date() > datetime(2026, 5, 24).date():
            return jsonify({"status": "success", "message": "Festival terminé. Aucune mise à jour nécessaire."}), 200

        # Appel à WeatherAPI pour les 14 prochains jours
        params = {
            'key': Config.WEATHER_API_KEY.strip(),
            'q': 'Houthalen-Helchteren',
            'days': 14,  # 14 jours de prévisions (max pour WeatherAPI)
            'lang': 'fr'
        }

        logger.info(f"Appel à WeatherAPI avec params: {params}")
        response = requests.get('http://api.weatherapi.com/v1/forecast.json', params=params)
        response.raise_for_status()
        weather_data = response.json()

        # Log des dates reçues pour débogage
        received_dates = [forecast["date"] for forecast in weather_data["forecast"]["forecastday"]]
        logger.info(f"Dates reçues de WeatherAPI: {received_dates}")

        # Filtre les données pour ne garder que les jours du festival (22-24 mai)
        weather_forecasts = []
        for forecast in weather_data["forecast"]["forecastday"]:
            date_str = forecast["date"]
            if date_str in Config.FESTIVAL_DAYS:
                day_data = forecast["day"]
                weather_forecasts.append({
                    "date": date_str,
                    "day_name": datetime.strptime(date_str, "%Y-%m-%d").strftime("%A"),
                    "temperature": day_data["avgtemp_c"],
                    "description": day_data["condition"]["text"],
                    "icon": f"https:{day_data['condition']['icon']}",
                    "humidity": day_data["avghumidity"],
                    "wind_speed": day_data["maxwind_kph"] / 3.6,  # Conversion km/h → m/s
                    "festival_day": Config.FESTIVAL_DAYS[date_str],
                })

        if not weather_forecasts:
            logger.error("Aucune date ne correspond aux jours du festival.")
            return jsonify({"status": "error", "message": "Aucune date ne correspond aux jours du festival."}), 500

        # Stocke dans BigQuery (écrase les anciennes données pour ces jours)
        store_weather_forecast(weather_forecasts)
        logger.info(f"Météo stockée pour {len(weather_forecasts)} jours.")
        return jsonify({"status": "success", "message": f"Météo stockée pour {len(weather_forecasts)} jours."}), 200

    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur WeatherAPI: {str(e)}")
        return jsonify({"status": "error", "message": f"Erreur WeatherAPI: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Erreur dans /update-weather: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/weather', methods=['GET'])
def get_weather():
    """
    Récupère les prévisions météo pour les 3 jours du festival.
    Appelé par ton appli Flutter.
    """
    try:
        forecasts = get_weather_forecast()
        # Convertis les dates en format ISO pour Flutter (optionnel)
        for forecast in forecasts:
            if isinstance(forecast['date'], datetime):
                forecast['date'] = forecast['date'].isoformat()
        return jsonify(forecasts), 200
    except Exception as e:
        logger.error(f"Erreur dans /weather: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)