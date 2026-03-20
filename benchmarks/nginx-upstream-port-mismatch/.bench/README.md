# nginx-upstream-port-mismatch

## Difficulty
Medium

## Source
Community-submitted

## Environment
Python 3.12, Flask, nginx, Debian slim

## The bug
The nginx reverse proxy config (`nginx/app.conf`) forwards `/api/` requests to `http://127.0.0.1:5001`, but the Flask backend listens on port 5000. The `/health` endpoint is a static nginx stub that never hits the upstream, so health checks always pass while all real API requests return 502 Bad Gateway.

## Why Medium
Requires understanding the interaction between nginx reverse proxy configuration and the Flask backend. The misleading health check (which passes despite the upstream being unreachable) adds a layer of misdirection. The agent must trace the request path through the nginx config, notice the port mismatch, and correlate it with the Flask server's actual listen port.

## Expected fix
Change `proxy_pass http://127.0.0.1:5001` to `proxy_pass http://127.0.0.1:5000` in `nginx/app.conf`.

## Pinned at
Anonymized snapshot, original repo not disclosed
