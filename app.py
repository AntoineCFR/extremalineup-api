from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas
from bigquery import (
    get_bigquery_timetable,
    get_user_favorite_set_ids,
    update_user_favorites,
    get_user_id
)
from config import Config

app = Flask(__name__)
CORS(app)

@app.route('/timetable', methods=['GET'])
def get_timetable():
    try:
        df = get_bigquery_timetable()
        return jsonify(df.to_dict(orient='records'))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/favorites', methods=['GET'])
def get_favorites():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        # Convertit user_id en INT64
        user_id_int = int(user_id)
        favorite_set_ids = get_user_favorite_set_ids(user_id_int)
        return jsonify({"favorites": [{"set_id": sid} for sid in favorite_set_ids]})
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/favorites', methods=['POST'])
def save_favorites():
    data = request.get_json()
    user_id = data.get('user_id')
    favorites_list = data.get('favorites', [])

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        # Convertit user_id en INT64
        user_id_int = int(user_id)
        update_user_favorites(user_id_int, favorites_list)
        return jsonify({"status": "success"})
    except ValueError:
        return jsonify({"error": "user_id must be an integer"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/users/check', methods=['GET'])
def check_user():
    username = request.args.get('username')
    if not username:
        return jsonify({"error": "username is required"}), 400

    try:
        user_id = get_user_id(username)  # Récupère l'ID de l'utilisateur
        if user_id is None:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "user_id": user_id})  # Retourne l'ID si l'utilisateur existe
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)