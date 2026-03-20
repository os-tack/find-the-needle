#!/bin/sh
set -e

# Start the Flask app in the background
python3 /workspace/app/server.py &
FLASK_PID=$!

# Start nginx in the foreground-daemon mode
nginx -g 'daemon off;' &
NGINX_PID=$!

# Wait for Flask to be ready (up to 10 seconds)
for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:5000/health >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# Wait for nginx to be ready (up to 5 seconds)
for i in $(seq 1 10); do
    if curl -s http://127.0.0.1:80/health >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

echo "Services started: flask=$FLASK_PID nginx=$NGINX_PID"

# Keep the script alive so CMD doesn't exit
wait
