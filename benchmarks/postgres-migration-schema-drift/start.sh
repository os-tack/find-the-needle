#!/bin/sh
set -e

PGDATA=/var/lib/postgresql/data

# Initialize PostgreSQL if needed
if [ ! -f "$PGDATA/PG_VERSION" ]; then
    su postgres -c "initdb -D $PGDATA"
fi

# Start PostgreSQL
su postgres -c "pg_ctl -D $PGDATA -l /var/log/postgresql.log start"

# Wait for PostgreSQL to be ready
for i in $(seq 1 30); do
    if su postgres -c "pg_isready" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Create database if it doesn't exist
su postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='ordersdb'\"" | grep -q 1 || \
    su postgres -c "createdb ordersdb"

# Apply migrations in order
for migration in /workspace/migrations/*.sql; do
    echo "Applying migration: $migration"
    su postgres -c "psql -d ordersdb -f $migration" 2>/dev/null || true
done

echo "Database ready"

# Start the Go API server
exec /workspace/orders-api
