from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas
from bigquery import get_bigquery_timetable, get_user_favorites, update_user_favorites
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
        favorite_set_ids = get_user_favorites(user_id)
        return jsonify({"favorites": [{"set_id": sid} for sid in favorite_set_ids]})
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
        update_user_favorites(user_id, favorites_list)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)