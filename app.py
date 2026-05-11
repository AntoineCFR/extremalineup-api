from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas
from bigquery import get_bigquery_timetable, get_user_favorites, toggle_favorite
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

@app.route('/favorites/<user_id>', methods=['GET'])
def get_favorites(user_id):
    try:
        df = get_user_favorites(user_id)
        return jsonify(df.to_dict(orient='records'))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/favorites', methods=['POST'])
def update_favorite():
    try:
        data = request.json
        user_id = data['user_id']
        set_id = data['set_id']
        toggle_favorite(user_id, set_id)
        return jsonify({"status": "success"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT)