from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
from datetime import datetime, timedelta
import requests
import logging
from bigquery import (
    get_bigquery_timetable,
    get_bigquery_user_favorites,
    toggle_bigquery_user_favorite,
    update_bigquery_user_favorite_notation,
    get_bigquery_user_id,
    store_bigquery_weather_forecast,
    get_bigquery_weather_forecast,
    get_bigquery_users,
    update_bigquery_user_phone,
    update_bigquery_user_location,
    get_bigquery_district,
    get_bigquery_districts,
    update_bigquery_district
)
from config import Config

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

@app.route('/users/check', methods=['GET'])
def check_user():
    username = request.args.get('username')
    if not username:
        return jsonify({"error": "username is required"}), 400

    try:
        user_id = get_bigquery_user_id(username)
        if user_id is None:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "user_id": int(user_id)})
    except Exception as e:
        logger.error(f"Erreur dans /users/check: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/update-weather', methods=['POST'])
def update_weather():
    try:
        if datetime.now().date() > datetime(2026, 5, 24).date():
            return jsonify({"status": "success", "message": "Festival terminé. Aucune mise à jour nécessaire."}), 200

        params = {
            'key': Config.WEATHER_API_KEY.strip(),
            'q': 'Houthalen-Helchteren',
            'days': 14,
            'lang': 'fr'
        }

        logger.info(f"Appel à WeatherAPI avec params: {params}")
        response = requests.get('http://api.weatherapi.com/v1/forecast.json', params=params)
        response.raise_for_status()
        weather_data = response.json()

        received_dates = [forecast["date"] for forecast in weather_data["forecast"]["forecastday"]]
        logger.info(f"Dates reçues de WeatherAPI: {received_dates}")

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
                    "wind_speed": day_data["maxwind_kph"] / 3.6,
                    "festival_day": Config.FESTIVAL_DAYS[date_str],
                })

        if not weather_forecasts:
            logger.error("Aucune date ne correspond aux jours du festival.")
            return jsonify({"status": "error", "message": "Aucune date ne correspond aux jours du festival."}), 500

        store_bigquery_weather_forecast(weather_forecasts)
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
    try:
        forecasts = get_bigquery_weather_forecast()
        for forecast in forecasts:
            if isinstance(forecast['date'], datetime):
                forecast['date'] = forecast['date'].isoformat()
        return jsonify(forecasts), 200
    except Exception as e:
        logger.error(f"Erreur dans /weather: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/users', methods=['GET'])
def get_users():
    """Récupère la liste de tous les utilisateurs."""
    try:
        users = get_bigquery_users()
        return jsonify(users), 200
    except Exception as e:
        logger.error(f"Erreur dans /users: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/<int:user_id>/phone', methods=['POST'])
def update_user_phone(user_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400

    phone_number = data.get('phone_number')
    if not phone_number:
        return jsonify({"error": "phone_number is required"}), 400

    try:
        update_bigquery_user_phone(user_id, phone_number)
        return jsonify({
            "status": "success",
            "message": "Numéro de téléphone mis à jour."
        }), 200
    except Exception as e:
        logger.error(f"Erreur dans /users/{user_id}/phone: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/<int:user_id>/location', methods=['POST'])
def update_user_location(user_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400

    lat = data.get('lat')
    lng = data.get('lng')

    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400

    try:
        update_bigquery_user_location(user_id, lat, lng)
        return jsonify({
            "status": "success",
            "message": "Localisation mise à jour."
        }), 200
    except Exception as e:
        logger.error(f"Erreur dans /users/{user_id}/location: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ========== NOUVEAUX ENDPOINTS POUR LES FAVORIS ==========

@app.route('/api/user-favorites', methods=['GET'])
def get_user_favorites():
    """
    Récupère les favoris depuis BigQuery.
    - Si user_id est fourni en query param : retourne les favoris de CET utilisateur
      Ex: /api/user-favorites?user_id=1 → [{"set_id": 1, "isfavorite": true, "notation": 5}, ...]
    - Si user_id N'EST PAS fourni : retourne TOUS les favoris de TOUS les utilisateurs
      Ex: /api/user-favorites → [{"user_id": 1, "set_id": 42, "isfavorite": true, "notation": 5}, ...]
    """
    try:
        user_id_param = request.args.get('user_id')
        user_id = int(user_id_param) if user_id_param else None
        favorites = get_bigquery_user_favorites(user_id)
        return jsonify({"favorites": favorites}), 200
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/user-favorites/toggle', methods=['POST'])
def toggle_favorite():
    """
    Toggle isfavorite pour un couple (user_id, set_id).
    Body JSON attendu: {"user_id": 1, "set_id": 42}
    Retourne: {"success": true, "isfavorite": true}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400

    user_id = data.get('user_id')
    set_id = data.get('set_id')

    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400

    try:
        user_id_int = int(user_id)
        set_id_int = int(set_id)
        new_value = toggle_bigquery_user_favorite(user_id_int, set_id_int)
        return jsonify({"success": True, "isfavorite": new_value}), 200
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites/toggle: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/user-favorites/rate', methods=['POST'])
def rate_favorite():
    """
    Met à jour la notation pour un couple (user_id, set_id).
    Body JSON attendu: {"user_id": 1, "set_id": 42, "notation": 5}
    notation peut être null pour supprimer la note.
    Retourne: {"success": true}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400

    user_id = data.get('user_id')
    set_id = data.get('set_id')
    notation = data.get('notation')

    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400

    try:
        user_id_int = int(user_id)
        set_id_int = int(set_id)
        notation_int = int(notation) if notation is not None else None
        update_bigquery_user_favorite_notation(user_id_int, set_id_int, notation_int)
        return jsonify({"success": True}), 200
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers, notation must be integer or null"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites/rate: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ========== NOUVEAUX ENDPOINTS POUR LES DISTRICTS ==========

@app.route('/api/districts', methods=['GET'])
def get_districts():
    """Récupère tous les districts."""
    try:
        districts = get_bigquery_districts()
        return jsonify(districts), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/districts: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/districts/<district_name>', methods=['GET'])
def get_district(district_name):
    """Récupère un district spécifique."""
    try:
        district = get_bigquery_district(district_name)
        if district is None:
            return jsonify({"error": "District non trouvé"}), 404
        return jsonify(district), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/districts/{district_name}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/districts/<district_name>', methods=['PUT'])
def update_district(district_name):
    """Met à jour les coordonnées d'un district."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400

    required_fields = [
        'lat_avg', 'lon_avg', 'lat_avd', 'lon_avd',
        'lat_arg', 'lon_arg', 'lat_ard', 'lon_ard',
        'lat_rally_point', 'lon_rally_point'
    ]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Le champ {field} est requis"}), 400

    try:
        district_data = {'district': district_name, **data}
        update_bigquery_district(district_data)
        return jsonify({"status": "success", "message": "District mis à jour."}), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/districts/{district_name}: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)