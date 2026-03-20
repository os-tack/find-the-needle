"""Webhook proxy — accepts a URL and proxies the request outward.

Intended to let customers configure webhook callbacks that fire on
certain events.  The server validates the destination URL before making
the request so that internal services are not exposed.
"""

import json
import traceback

from flask import Flask, request, jsonify
import requests as http_client

from validator import is_url_allowed

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook/proxy", methods=["POST"])
def webhook_proxy():
    """Proxy an outbound request on behalf of the caller.

    Expects JSON: {"url": "<destination>", "method": "GET"|"POST", ...}
    """
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"error": "missing 'url' in request body"}), 400

    url = body["url"]
    method = body.get("method", "GET").upper()

    # --- URL validation ---------------------------------------------------
    if not is_url_allowed(url):
        return jsonify({"error": "URL is not allowed", "blocked": True}), 403

    # --- Proxy the request ------------------------------------------------
    try:
        if method == "GET":
            resp = http_client.get(url, timeout=5)
        elif method == "POST":
            resp = http_client.post(url, json=body.get("payload", {}), timeout=5)
        else:
            return jsonify({"error": f"unsupported method: {method}"}), 400

        # Return upstream response to the caller
        try:
            upstream_json = resp.json()
        except Exception:
            upstream_json = None

        return jsonify({
            "status_code": resp.status_code,
            "body": upstream_json if upstream_json is not None else resp.text,
        }), 200

    except http_client.ConnectionError:
        return jsonify({"error": "connection failed"}), 502
    except http_client.Timeout:
        return jsonify({"error": "upstream timeout"}), 504
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
