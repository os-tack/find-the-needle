"""
Transfer processor service.
Runs on port 8081 and handles the actual fund transfer logic.

Receives transfer requests from the gateway and executes them
against the shared database. Includes idempotency checking to
prevent duplicate transfers.
"""

import sqlite3
import time
from flask import Flask, request, jsonify
from models import get_db

app = Flask(__name__)


def execute_transfer(from_acct, to_acct, amount, idempotency_key):
    """
    Execute a fund transfer between two accounts.

    This function handles the core transfer logic including:
    - Sufficient balance checking
    - Debiting the source account
    - Crediting the destination account
    - Recording the transfer for idempotency

    The idempotency key prevents the same logical transfer from being
    executed more than once, which is critical when the gateway retries
    requests due to timeouts.
    """
    conn = get_db()
    try:
        # Check if source account has sufficient funds
        row = conn.execute(
            "SELECT balance FROM accounts WHERE name = ?",
            (from_acct,)
        ).fetchone()

        if row is None:
            return {"status": "error", "message": f"account {from_acct} not found"}

        if row["balance"] < amount:
            return {"status": "error", "message": "insufficient funds"}

        # Debit source account
        conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE name = ?",
            (amount, from_acct)
        )
        conn.commit()

        # Small delay to simulate processing / cross-service call.
        # In production this might be a call to a fraud-check service
        # or a compliance verification step.
        time.sleep(0.05)

        # Check idempotency — if we've already processed this transfer,
        # roll back the debit and return the cached result.
        existing = conn.execute(
            "SELECT result FROM completed_transfers WHERE idempotency_key = ?",
            (idempotency_key,)
        ).fetchone()

        if existing is not None:
            # Already processed — undo the debit we just did
            conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE name = ?",
                (amount, from_acct)
            )
            conn.commit()
            return {"status": "duplicate", "message": "transfer already completed"}

        # Credit destination account
        conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE name = ?",
            (amount, to_acct)
        )

        # Record the completed transfer for idempotency
        conn.execute(
            "INSERT INTO completed_transfers (idempotency_key, from_account, to_account, amount, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, from_acct, to_acct, amount, "success")
        )
        conn.commit()

        return {"status": "success", "message": f"transferred {amount} from {from_acct} to {to_acct}"}

    except sqlite3.IntegrityError:
        # Idempotency key constraint violation — another thread beat us
        # to the insert. Undo the debit.
        conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE name = ?",
            (amount, from_acct)
        )
        conn.commit()
        return {"status": "duplicate", "message": "transfer already completed (race)"}

    except Exception as e:
        # On any error, try to undo the debit
        try:
            conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE name = ?",
                (amount, from_acct)
            )
            conn.commit()
        except Exception:
            pass
        return {"status": "error", "message": str(e)}

    finally:
        conn.close()


@app.route("/execute", methods=["POST"])
def handle_execute():
    """Handle transfer execution requests from the gateway."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "missing JSON body"}), 400

    required = ["from", "to", "amount", "idempotency_key"]
    for field in required:
        if field not in data:
            return jsonify({"status": "error", "message": f"missing field: {field}"}), 400

    result = execute_transfer(
        data["from"],
        data["to"],
        int(data["amount"]),
        data["idempotency_key"]
    )
    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
