"""
Chaos proxy for testing resilience.
Runs on port 8082 and forwards requests to the processor on port 8081.

The proxy can be configured to drop/delay responses to simulate network
issues. The request always goes through to the backend; only the
response is affected. This simulates real-world scenarios where the
server processes the request but the response is lost in transit.
"""

import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Chaos configuration — set via /config endpoint
_config_lock = threading.Lock()
_drop_response_ms = 0  # If > 0, delay responses by this many ms


@app.route("/config", methods=["POST"])
def configure():
    """
    Configure chaos behavior.

    JSON body:
        drop_response_ms: int — delay response delivery by N milliseconds.
                                The request is forwarded immediately; only
                                the response is held, simulating a network
                                partition on the return path.
    """
    global _drop_response_ms
    data = request.get_json() or {}

    with _config_lock:
        if "drop_response_ms" in data:
            _drop_response_ms = int(data["drop_response_ms"])

    return jsonify({
        "status": "configured",
        "drop_response_ms": _drop_response_ms
    })


@app.route("/execute", methods=["POST"])
def proxy_execute():
    """
    Forward transfer requests to the processor.

    The request is always forwarded immediately. If chaos is configured,
    the response is delayed to simulate network issues. This causes the
    gateway to time out and retry, even though the processor already
    committed the transfer.
    """
    data = request.get_json()

    # Forward to the real processor
    try:
        resp = requests.post(
            "http://localhost:8081/execute",
            json=data,
            timeout=30
        )
        result = resp.json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"proxy error: {e}"}), 502

    # Apply chaos: delay the response
    with _config_lock:
        delay_ms = _drop_response_ms

    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)

    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082)
