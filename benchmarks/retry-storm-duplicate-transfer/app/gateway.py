"""
Transfer gateway service.
Runs on port 8080 and provides the external API for fund transfers.

Routes transfer requests through the chaos proxy to the processor.
Implements retry logic to handle transient failures and timeouts.
"""

import requests
from flask import Flask, request, jsonify
from models import get_db

app = Flask(__name__)

# The gateway forwards transfer requests through the chaos proxy,
# which simulates network unreliability.
PROCESSOR_URL = "http://localhost:8082/execute"

# Retry configuration for resilience
MAX_RETRIES = 3
RETRY_TIMEOUT_SECONDS = 1


@app.route("/transfer", methods=["POST"])
def transfer():
    """
    Initiate a fund transfer.

    Forwards the request to the processor service (via the chaos proxy)
    with automatic retries on timeout. The idempotency key in the request
    ensures that retries don't cause duplicate transfers.
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "missing JSON body"}), 400

    required = ["from", "to", "amount", "idempotency_key"]
    for field in required:
        if field not in data:
            return jsonify({"status": "error", "message": f"missing field: {field}"}), 400

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                PROCESSOR_URL,
                json=data,
                timeout=RETRY_TIMEOUT_SECONDS
            )
            result = resp.json()
            return jsonify(result)

        except requests.exceptions.Timeout:
            last_error = f"attempt {attempt}: timeout after {RETRY_TIMEOUT_SECONDS}s"
            app.logger.warning(
                "Transfer %s attempt %d timed out, retrying...",
                data.get("idempotency_key", "?"),
                attempt
            )
            continue

        except requests.exceptions.ConnectionError as e:
            last_error = f"attempt {attempt}: connection error: {e}"
            app.logger.warning(
                "Transfer %s attempt %d connection error: %s",
                data.get("idempotency_key", "?"),
                attempt,
                e
            )
            continue

    return jsonify({
        "status": "error",
        "message": f"all {MAX_RETRIES} attempts failed: {last_error}"
    }), 503


@app.route("/balance/<account>")
def balance(account):
    """Get the current balance for an account."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT balance FROM accounts WHERE name = ?",
            (account,)
        ).fetchone()
        if row is None:
            return jsonify({"status": "error", "message": "account not found"}), 404
        return jsonify({"account": account, "balance": row["balance"]})
    finally:
        conn.close()


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
