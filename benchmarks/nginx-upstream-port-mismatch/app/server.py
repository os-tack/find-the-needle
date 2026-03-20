from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/data")
def api_data():
    return jsonify({
        "status": "ok",
        "items": [
            {"id": 1, "name": "widget-a", "price": 9.99},
            {"id": 2, "name": "widget-b", "price": 14.50},
            {"id": 3, "name": "widget-c", "price": 22.00},
        ],
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
