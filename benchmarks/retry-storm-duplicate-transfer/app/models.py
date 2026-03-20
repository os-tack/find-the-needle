"""
Database models and schema for the banking service.
Uses a shared SQLite database for accounts and transfer tracking.
"""

import sqlite3
import os

DB_PATH = "/tmp/bank.db"


def get_db():
    """Get a connection to the shared database."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            name TEXT PRIMARY KEY,
            balance INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS completed_transfers (
            idempotency_key TEXT PRIMARY KEY,
            from_account TEXT NOT NULL,
            to_account TEXT NOT NULL,
            amount INTEGER NOT NULL,
            result TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


def seed():
    """Seed the database with initial accounts."""
    init_schema()
    conn = get_db()
    # Clear and re-seed
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM completed_transfers")
    conn.execute("INSERT INTO accounts (name, balance) VALUES ('A', 1000)")
    conn.execute("INSERT INTO accounts (name, balance) VALUES ('B', 1000)")
    conn.commit()
    conn.close()
    print("Seeded accounts: A=1000, B=1000")


if __name__ == "__main__":
    seed()
