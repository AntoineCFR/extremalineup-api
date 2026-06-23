from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import math
import hmac
from datetime import datetime, date, time
from zoneinfo import ZoneInfo
import requests
import logging
from bigquery import (
    get_bigquery_festivals,
    get_bigquery_festival,
    get_bigquery_timetable,
    get_bigquery_user_favorites,
    toggle_bigquery_user_favorite,
    update_bigquery_user_favorite_notation,
    normalize_tag,
    get_bigquery_dj_tags,
    add_bigquery_dj_tag,
    delete_bigquery_dj_tag,
    get_bigquery_user_id,
    store_bigquery_weather_forecast,
    get_bigquery_weather_forecast,
    get_bigquery_users,
    update_bigquery_user_phone,
    update_bigquery_user_location,
    update_bigquery_user_tent,
    get_bigquery_stage,
    get_bigquery_stages,
    update_bigquery_stage,
    insert_bigquery_geoloc,
    get_stage_from_coordinates,
    update_bigquery_user_location_and_stage,
    insert_bigquery_event,
    delete_last_bigquery_event,
    update_all_users_stage,
    get_bigquery_user_events,
    get_journal,
    insert_notification,
    sync_timetable_festival,
    select_new_sets,
    journal_lineup_changes,
    get_unpushed_programmation,
    mark_notification_pushed,
    ensure_stages_exist,
    MassCancellationError,
)
from config import Config
from scrapers import SCRAPERS
from firebase_cloud_messaging import (
    send_sos_notification,
    send_perdu_notification,
    send_hype_notification,
    send_topic_notification,
)
import push_schedule
import firebase_admin
from firebase_admin import credentials as firebase_credentials

# Initialisation du SDK Firebase Admin (envoi des notifications push).
# Utilise le credential Firebase (projet `festcompanion`), distinct du SA
# BigQuery, sinon les push n'atteignent aucun appareil.
if not firebase_admin._apps:
    cred = firebase_credentials.Certificate(Config.FIREBASE_CREDENTIALS)
    firebase_admin.initialize_app(cred)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# --- Helpers festival_id -----------------------------------------------------

def _festival_id_from_args():
    """Lit festival_id depuis les query params. Retourne (id, None) ou (None, erreur_str)."""
    raw = request.args.get('festival_id')
    if raw is None or raw == '':
        return None, "festival_id is required"
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "festival_id must be an integer"

def _festival_id_from_body(data):
    """Lit festival_id depuis un body JSON. Retourne (id, None) ou (None, erreur_str)."""
    raw = data.get('festival_id') if data else None
    if raw is None:
        return None, "festival_id is required"
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "festival_id must be an integer"

def _nan_to_none(records):
    """Remplace les float NaN par None dans une liste de dicts → JSON `null` valide.
    Robuste (contrairement à df.where) : agit après to_dict, donc insensible au
    type des colonnes pandas (les colonnes tout-NULL en float64 sont bien gérées)."""
    for rec in records:
        for key, value in rec.items():
            if isinstance(value, float) and math.isnan(value):
                rec[key] = None
    return records

def _festival_utc_offset(festival):
    """Décalage UTC (timedelta) du festival, calculé à partir de son fuseau IANA.
    Remplace l'ancien +2h codé en dur."""
    tz = ZoneInfo(festival["timezone"])
    ref = datetime.combine(date.fromisoformat(festival["start_date"]), time(12, 0))
    return tz.utcoffset(ref)


# ========== FESTIVALS ==========

@app.route('/api/festivals', methods=['GET'])
def get_festivals():
    """Liste des festivals (pour l'écran de sélection)."""
    try:
        active_param = request.args.get('active_only', 'true').lower() != 'false'
        festivals = get_bigquery_festivals(active_only=active_param)
        return jsonify(festivals), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/festivals: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/festivals/<int:festival_id>', methods=['GET'])
def get_festival(festival_id):
    """Détails d'un festival."""
    try:
        festival = get_bigquery_festival(festival_id)
        if festival is None:
            return jsonify({"error": "Festival non trouvé"}), 404
        return jsonify(festival), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/festivals/{festival_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== TIMETABLE ==========

@app.route('/timetable', methods=['GET'])
def get_timetable():
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        festival = get_bigquery_festival(festival_id)
        if festival is None:
            return jsonify({"error": "Festival non trouvé"}), 404

        df = get_bigquery_timetable(festival_id)
        offset = _festival_utc_offset(festival)
        df['start_time'] += offset
        df['end_time'] += offset
        # Remplace les NaN (colonnes NULL en base) par None → JSON `null` valide.
        # Sinon pandas/jsonify produit `NaN`, que Dart refuse de parser.
        records = _nan_to_none(df.to_dict(orient='records'))
        return jsonify(records)
    except Exception as e:
        logger.error(f"Erreur dans /timetable: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== UTILISATEURS ==========

@app.route('/users/check', methods=['GET'])
def check_user():
    """Vérifie l'existence d'un utilisateur (compte global, pas lié à un festival)."""
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

@app.route('/users', methods=['GET'])
def get_users():
    """Liste des utilisateurs présents sur un festival (avec leur position)."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        users = get_bigquery_users(festival_id)
        return jsonify(users), 200
    except Exception as e:
        logger.error(f"Erreur dans /users: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/<int:user_id>/phone', methods=['POST'])
def update_user_phone(user_id):
    """Met à jour le numéro de téléphone (donnée globale du compte)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    phone_number = data.get('phone_number')
    if not phone_number:
        return jsonify({"error": "phone_number is required"}), 400
    try:
        update_bigquery_user_phone(user_id, phone_number)
        return jsonify({"status": "success", "message": "Numéro de téléphone mis à jour."}), 200
    except Exception as e:
        logger.error(f"Erreur dans /users/{user_id}/phone: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/<int:user_id>/location', methods=['POST'])
def update_user_location(user_id):
    """Met à jour la position d'un utilisateur sur un festival donné."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    try:
        update_bigquery_user_location(festival_id, user_id, lat, lng)
        return jsonify({"status": "success", "message": "Localisation mise à jour."}), 200
    except Exception as e:
        logger.error(f"Erreur dans /users/{user_id}/location: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/users/<int:user_id>/tent', methods=['POST'])
def update_user_tent(user_id):
    """Enregistre l'emplacement de la tente (campement) d'un utilisateur sur un
    festival donné. Distinct de la position courante."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng are required"}), 400
    try:
        update_bigquery_user_tent(festival_id, user_id, lat, lng)
        return jsonify({"status": "success", "message": "Emplacement de la tente mis à jour."}), 200
    except Exception as e:
        logger.error(f"Erreur dans /users/{user_id}/tent: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== MÉTÉO ==========

# GET autorisé en plus de POST : permet de déclencher la MAJ depuis un simple
# cron (cron-job.org…) ou le navigateur, sans avoir à forger une requête POST.
@app.route('/update-weather', methods=['POST', 'GET'])
def update_weather():
    """Met à jour la météo de tous les festivals à venir / en cours.
    La ville et les dates proviennent de la table `festivals`."""
    try:
        today = datetime.now().date()
        festivals = get_bigquery_festivals(active_only=True)
        upcoming = [f for f in festivals if f["end_date"] and date.fromisoformat(f["end_date"]) >= today]

        if not upcoming:
            return jsonify({"status": "success", "message": "Aucun festival à venir. Rien à mettre à jour."}), 200

        results = []
        for festival in upcoming:
            start = date.fromisoformat(festival["start_date"])
            end = date.fromisoformat(festival["end_date"])

            params = {
                'key': Config.WEATHER_API_KEY.strip(),
                'q': festival["city"],
                'days': 14,
                'lang': 'fr',
            }
            response = requests.get('http://api.weatherapi.com/v1/forecast.json', params=params)
            response.raise_for_status()
            weather_data = response.json()

            weather_forecasts = []
            for forecast in weather_data["forecast"]["forecastday"]:
                date_str = forecast["date"]
                forecast_date = date.fromisoformat(date_str)
                if start <= forecast_date <= end:
                    day_data = forecast["day"]
                    weather_forecasts.append({
                        "date": date_str,
                        "day_name": forecast_date.strftime("%A"),
                        "temperature": day_data["avgtemp_c"],
                        "description": day_data["condition"]["text"],
                        "icon": f"https:{day_data['condition']['icon']}",
                        "humidity": day_data["avghumidity"],
                        "wind_speed": day_data["maxwind_kph"] / 3.6,
                        "festival_day": forecast_date.strftime("%A").lower(),
                    })

            if weather_forecasts:
                store_bigquery_weather_forecast(festival["festival_id"], weather_forecasts)
                results.append({"festival_id": festival["festival_id"], "days": len(weather_forecasts)})
                logger.info(f"Météo stockée pour le festival {festival['festival_id']} ({len(weather_forecasts)} jours).")

        return jsonify({"status": "success", "updated": results}), 200

    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur WeatherAPI: {str(e)}")
        return jsonify({"status": "error", "message": f"Erreur WeatherAPI: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Erreur dans /update-weather: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/weather', methods=['GET'])
def get_weather():
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        forecasts = _nan_to_none(get_bigquery_weather_forecast(festival_id))
        for forecast in forecasts:
            if isinstance(forecast['date'], datetime):
                forecast['date'] = forecast['date'].isoformat()
        return jsonify(forecasts), 200
    except Exception as e:
        logger.error(f"Erreur dans /weather: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ========== FAVORIS ==========

@app.route('/api/user-favorites', methods=['GET'])
def get_user_favorites():
    """Favoris d'un festival.
    - avec user_id : favoris de CET utilisateur → [{set_id, isfavorite, notation}, ...]
    - sans user_id : tous les favoris → [{user_id, set_id, isfavorite, notation}, ...]
    """
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        user_id_param = request.args.get('user_id')
        user_id = int(user_id_param) if user_id_param else None
        favorites = get_bigquery_user_favorites(festival_id, user_id)
        return jsonify({"favorites": favorites}), 200
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/user-favorites/toggle', methods=['POST'])
def toggle_favorite():
    """Toggle isfavorite. Body: {"festival_id": 1, "user_id": 1, "set_id": 42}"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    set_id = data.get('set_id')
    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400
    try:
        new_value = toggle_bigquery_user_favorite(festival_id, int(user_id), int(set_id))
        return jsonify({"success": True, "isfavorite": new_value}), 200
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites/toggle: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/user-favorites/rate', methods=['POST'])
def rate_favorite():
    """Met à jour la notation. Body: {"festival_id": 1, "user_id": 1, "set_id": 42, "notation": 5}.
    notation peut être null pour supprimer la note."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    set_id = data.get('set_id')
    notation = data.get('notation')
    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400
    try:
        notation_int = int(notation) if notation is not None else None
        update_bigquery_user_favorite_notation(festival_id, int(user_id), int(set_id), notation_int)
        return jsonify({"success": True}), 200
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers, notation must be integer or null"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/user-favorites/rate: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== TAGS DJ ==========

@app.route('/api/dj-tags', methods=['GET'])
def get_dj_tags():
    """Tags d'un festival.
    - avec set_id : tags de CE set → [{user_id, set_id, tag}, ...]
    - sans set_id : tous les tags du festival (cache + page « DJ par tag »).
    """
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        set_id_param = request.args.get('set_id')
        set_id = int(set_id_param) if set_id_param else None
        tags = get_bigquery_dj_tags(festival_id, set_id)
        return jsonify({"tags": tags}), 200
    except ValueError:
        return jsonify({"error": "set_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur dans /api/dj-tags: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dj-tags', methods=['POST'])
def add_dj_tag():
    """Ajoute un tag. Body: {"festival_id": 1, "user_id": 1, "set_id": 42, "tag": "techno"}.
    Le tag est normalisé côté serveur (sans espace, sans #, minuscule)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    set_id = data.get('set_id')
    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400
    tag = normalize_tag(data.get('tag'))
    if not tag:
        return jsonify({"error": "tag is required"}), 400
    try:
        add_bigquery_dj_tag(festival_id, int(user_id), int(set_id), tag)
        return jsonify({"success": True, "tag": tag}), 201
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers"}), 400
    except Exception as e:
        logger.error(f"Erreur dans POST /api/dj-tags: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dj-tags', methods=['DELETE'])
def delete_dj_tag():
    """Supprime SON propre tag. Body: {"festival_id": 1, "user_id": 1, "set_id": 42, "tag": "techno"}."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    set_id = data.get('set_id')
    if user_id is None or set_id is None:
        return jsonify({"error": "user_id and set_id are required"}), 400
    tag = normalize_tag(data.get('tag'))
    if not tag:
        return jsonify({"error": "tag is required"}), 400
    try:
        delete_bigquery_dj_tag(festival_id, int(user_id), int(set_id), tag)
        return jsonify({"success": True}), 200
    except ValueError:
        return jsonify({"error": "user_id and set_id must be integers"}), 400
    except Exception as e:
        logger.error(f"Erreur dans DELETE /api/dj-tags: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== SCÈNES (ex-districts) ==========

@app.route('/api/stages', methods=['GET'])
def get_stages():
    """Toutes les scènes d'un festival."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        stages = get_bigquery_stages(festival_id)
        return jsonify(stages), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/stages: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stages/<stage_name>', methods=['GET'])
def get_stage(stage_name):
    """Une scène spécifique."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        stage = get_bigquery_stage(festival_id, stage_name)
        if stage is None:
            return jsonify({"error": "Scène non trouvée"}), 404
        return jsonify(stage), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/stages/{stage_name}: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stages/<stage_name>', methods=['PUT'])
def update_stage(stage_name):
    """Met à jour les coordonnées d'une scène. festival_id attendu dans le body."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    required_fields = [
        'lat_avg', 'lon_avg', 'lat_avd', 'lon_avd',
        'lat_arg', 'lon_arg', 'lat_ard', 'lon_ard',
        'lat_rally_point', 'lon_rally_point',
    ]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Le champ {field} est requis"}), 400
    try:
        stage_data = {'stage': stage_name, **data}
        update_bigquery_stage(festival_id, stage_data)
        return jsonify({"status": "success", "message": "Scène mise à jour."}), 200
    except Exception as e:
        logger.error(f"Erreur dans /api/stages/{stage_name}: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== GÉOLOC ==========

@app.route('/api/geoloc', methods=['POST'])
def update_geoloc():
    """Met à jour la géoloc d'un utilisateur sur un festival et détermine sa scène.
    Body: {"festival_id": 1, "user_id": 1, "lat": .., "lng": ..}"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    lat = data.get('lat')
    lng = data.get('lng')
    if user_id is None or lat is None or lng is None:
        return jsonify({"error": "user_id, lat and lng are required"}), 400
    try:
        user_id_int = int(user_id)
        lat_float = float(lat)
        lng_float = float(lng)

        insert_bigquery_geoloc(festival_id, user_id_int, lat_float, lng_float)
        stage_name = get_stage_from_coordinates(festival_id, lat_float, lng_float)
        update_bigquery_user_location_and_stage(
            festival_id, user_id_int, lat_float, lng_float, stage_name if stage_name else "?"
        )
        return jsonify({"status": "success", "stage": stage_name if stage_name else "?"}), 200
    except ValueError:
        return jsonify({"error": "Types de données invalides"}), 400
    except Exception as e:
        logger.error(f"Erreur /api/geoloc: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== ÉVÉNEMENTS ==========

def _festival_is_ongoing(festival):
    """True si la date LOCALE du festival est dans [start_date, end_date] inclus.
    Sert à ne journaliser les événements temps réel que pendant le festival
    (ni avant, ni après)."""
    try:
        if not festival or not festival.get("start_date") or not festival.get("end_date"):
            return False
        tz = ZoneInfo(festival["timezone"]) if festival.get("timezone") else ZoneInfo("UTC")
        today_local = datetime.now(tz).date()
        start = date.fromisoformat(festival["start_date"])
        end = date.fromisoformat(festival["end_date"])
        return start <= today_local <= end
    except Exception as e:
        logger.error(f"Erreur _festival_is_ongoing: {e}")
        return False


def _journal_realtime_event(festival_id, user_id, theme, title, body, variant=None):
    """Journalise un événement temps réel (SOS/perdu/hype) dans `notifications`
    pour qu'il apparaisse dans l'écran Journal. `slot_key` unique par déclenchement
    (pas de dédoublonnage voulu, contrairement aux créneaux programmés).
    Best-effort : ne doit jamais casser la création de l'événement."""
    try:
        slot_key = f"rt|{theme}|{user_id}|{datetime.utcnow().isoformat()}"
        insert_notification(
            festival_id, slot_key, day=None, scheduled_local=None,
            theme=theme, title=title, body=body, pushed=True, variant=variant,
        )
    except Exception as e:
        logger.error(f"Journalisation événement temps réel échouée ({theme}): {e}")


@app.route('/api/events', methods=['POST'])
def create_event():
    """Crée un événement. Body: {"festival_id": 1, "user_id": 1, "event_type": "sos"}"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Aucune donnée fournie"}), 400
    festival_id, err = _festival_id_from_body(data)
    if err:
        return jsonify({"error": err}), 400
    user_id = data.get('user_id')
    event_type = data.get('event_type')
    if user_id is None or event_type is None:
        return jsonify({"error": "user_id and event_type are required"}), 400
    try:
        user_id_int = int(user_id)
        event_type_str = str(event_type)

        insert_bigquery_event(festival_id, user_id_int, event_type_str)

        # Mise à jour des scènes pour "perdu" (donnée importante → peut lever).
        if event_type_str == "perdu":
            update_all_users_stage(festival_id)

        # Les notifications push (et leur journalisation) ne doivent JAMAIS faire
        # échouer la création de l'événement (déjà persisté). On loggue et on
        # continue. La journalisation n'a lieu que PENDANT le festival ; le thème
        # 'perdu' est journalisé en 'lost' (icône Journal existante côté app).
        try:
            festival = get_bigquery_festival(festival_id)
            ongoing = _festival_is_ongoing(festival)
            if event_type_str == "perdu":
                title, body = send_perdu_notification(user_id_int)
                if ongoing:
                    _journal_realtime_event(festival_id, user_id_int, "lost", title, body)
            elif event_type_str == "sos":
                title, body = send_sos_notification(user_id_int)
                if ongoing:
                    _journal_realtime_event(festival_id, user_id_int, "sos", title, body)
            elif event_type_str == "hype":
                title, body, variant_key = send_hype_notification(user_id_int, festival_id)
                if ongoing:
                    _journal_realtime_event(festival_id, user_id_int, "hype", title, body, variant_key)
        except Exception as notif_err:
            logger.error(f"Notification push échouée (événement '{event_type_str}' créé malgré tout): {notif_err}")

        return jsonify({"status": "success", "event_type": event_type_str}), 201
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur /api/events: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/events/last', methods=['DELETE'])
def delete_last_event():
    """Supprime le dernier événement d'un utilisateur sur un festival."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    user_id_param = request.args.get('user_id')
    if not user_id_param:
        return jsonify({"error": "user_id is required"}), 400
    try:
        delete_last_bigquery_event(festival_id, int(user_id_param))
        return jsonify({"status": "success"}), 200
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur DELETE /api/events/last: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/events', methods=['GET'])
def get_events():
    """Événements d'un utilisateur sur un festival."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    user_id_param = request.args.get('user_id')
    if not user_id_param:
        return jsonify({"error": "user_id is required"}), 400
    try:
        events = get_bigquery_user_events(festival_id, int(user_id_param))
        return jsonify(events), 200
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur /api/events GET: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== PUSHS PROGRAMMÉS / JOURNAL ==========

# GET pour être déclenchable par un simple cron (cron-job.org). Idempotent :
# l'anti-doublon (table notifications) garantit un seul envoi par créneau, même
# si le cron tape plusieurs fois dans la même fenêtre. Festival_id OPTIONNEL :
# sans lui, on traite tous les festivals actifs dans leur fenêtre (1 seul cron).
@app.route('/api/push/tick', methods=['GET', 'POST'])
def push_tick():
    try:
        raw = request.args.get('festival_id')
        if raw:
            festival = get_bigquery_festival(int(raw))
            festivals = [festival] if festival else []
        else:
            festivals = get_bigquery_festivals(active_only=True)

        results = []
        for festival in festivals:
            if not festival or not festival.get("timezone") or not festival.get("start_date"):
                continue
            try:
                results.append(push_schedule.run_tick(festival))
            except Exception as fe:
                logger.error(f"Erreur tick festival {festival.get('festival_id')}: {fe}")
                results.append({"status": "error", "festival_id": festival.get("festival_id"),
                                "message": str(fe)})
        return jsonify({"status": "success", "results": results}), 200
    except ValueError:
        return jsonify({"error": "festival_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur /api/push/tick: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/journal', methods=['GET'])
def get_journal_route():
    """Toutes les notifications programmées d'un festival (page Journal)."""
    festival_id, err = _festival_id_from_args()
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify({"journal": get_journal(festival_id)}), 200
    except Exception as e:
        logger.error(f"Erreur /api/journal: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ========== ADMIN : RAFRAÎCHISSEMENT DU LINE-UP ==========

def _admin_authorized(req):
    """Vrai si la requête porte le bon secret admin. Secret transmis UNIQUEMENT
    par header (jamais en query, pour ne pas fuiter en logs/historique) :
      Authorization: Bearer <token>   ou   X-Admin-Token: <token>
    Comparaison en temps constant (anti-timing). Fail-closed si le secret n'est
    pas configuré côté serveur."""
    expected = Config.ADMIN_REFRESH_TOKEN
    if not expected:
        return False
    provided = req.headers.get('X-Admin-Token')
    if not provided:
        auth = req.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            provided = auth[len('Bearer '):].strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _push_pending_programmation(festival_id):
    """Pousse à `all_users` les changements de programmation pas encore poussés
    (1 push par changement), puis les marque pushed=TRUE. Idempotent : une push
    échouée repart au run suivant. Best-effort par entrée : un échec n'empêche
    pas les autres."""
    sent = 0
    for n in get_unpushed_programmation(festival_id):
        try:
            send_topic_notification(n["title"], n["body"], data={"event_type": "programmation"})
            mark_notification_pushed(festival_id, n["slot_key"])
            sent += 1
        except Exception as e:
            logger.error(f"Push programmation échoué ({n['slot_key']}): {e}")
    return sent


def _refresh_one_festival(festival, force, dry_run=False):
    """Scrape + diff (+ journal + push si pas dry_run) pour UN festival.
    Retourne un récap. En dry_run : calcule le plan, n'écrit/ne pousse RIEN."""
    festival_id = festival["festival_id"]
    adapter = SCRAPERS.get(festival_id)
    if adapter is None:
        return {"festival_id": festival_id, "skipped": "pas d'adaptateur de scraping"}

    # On ne rafraîchit jamais un festival terminé (sinon le site vide ferait tout
    # passer en « annulé » — bloqué par le garde-fou, mais autant ne pas scraper).
    today = datetime.now().date()
    if festival.get("end_date") and date.fromisoformat(festival["end_date"]) < today:
        return {"festival_id": festival_id, "skipped": "festival terminé"}

    tz = ZoneInfo(festival["timezone"]) if festival.get("timezone") else ZoneInfo("UTC")

    rows = adapter.scrape_rows(festival)
    df = pd.DataFrame(rows)
    if df.empty:
        return {"festival_id": festival_id, "skipped": "aucun set scrapé"}

    if dry_run:
        plan = sync_timetable_festival(df, festival_id, dry_run=True)
        counts = {}
        for c in plan:
            counts[c["type"]] = counts.get(c["type"], 0) + 1
        return {"festival_id": festival_id, "dry_run": True, "changes": counts}

    # Bios : uniquement pour les NOUVEAUX sets (jamais les artistes existants).
    new_mask = select_new_sets(df, festival_id)
    if bool(new_mask.any()):
        adapter.enrich_new_bios(festival, df, new_mask)

    applied = sync_timetable_festival(df, festival_id, dry_run=False, force=force)
    # Scènes surprises : on s'assure que toute scène scrapée a sa ligne `stages`
    # (additif, coords à 0 — ne touche jamais aux coordonnées déjà réglées).
    new_stages = ensure_stages_exist(festival_id, df["stage"].dropna().unique().tolist())
    journaled = journal_lineup_changes(festival_id, applied, tz)
    pushed = _push_pending_programmation(festival_id)

    counts = {}
    for c in applied:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    return {"festival_id": festival_id, "changes": counts, "new_stages": new_stages,
            "journaled": journaled, "pushed": pushed}


# GET et POST : déclenchable par cron-job.org (comme /update-weather, /push/tick).
# ⚠️ MODIFIE la base → protégé par un secret (ADMIN_REFRESH_TOKEN). Sans
# festival_id, traite tous les festivals actifs à venir/en cours qui ont un
# adaptateur. ?force=true passe outre le garde-fou anti-désactivation massive.
@app.route('/api/admin/refresh-lineup', methods=['GET', 'POST'])
def refresh_lineup():
    if not _admin_authorized(request):
        return jsonify({"error": "unauthorized"}), 403

    force = str(request.args.get('force', '')).lower() in ('1', 'true', 'yes')
    dry_run = str(request.args.get('dry_run', '')).lower() in ('1', 'true', 'yes')
    raw = request.args.get('festival_id')
    try:
        if raw:
            festival = get_bigquery_festival(int(raw))
            festivals = [festival] if festival else []
        else:
            festivals = get_bigquery_festivals(active_only=True)

        results = []
        for festival in festivals:
            if not festival:
                continue
            fid = festival.get("festival_id")
            try:
                results.append(_refresh_one_festival(festival, force, dry_run))
            except MassCancellationError as e:
                logger.error(f"Garde-fou refresh festival {fid}: {e}")
                results.append({"festival_id": fid, "error": "mass_cancellation_guard",
                                "detail": str(e)})
            except Exception as fe:
                logger.error(f"Erreur refresh festival {fid}: {fe}")
                results.append({"festival_id": fid, "error": str(fe)})
        return jsonify({"status": "success", "results": results}), 200
    except ValueError:
        return jsonify({"error": "festival_id must be an integer"}), 400
    except Exception as e:
        logger.error(f"Erreur /api/admin/refresh-lineup: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)
