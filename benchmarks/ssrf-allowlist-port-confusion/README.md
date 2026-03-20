# ssrf-allowlist-port-confusion

## Project

A Python Flask webhook proxy that accepts a destination URL and proxies the request outward. Before making the request it validates the URL against a blocklist of internal/private addresses so the proxy cannot be used for SSRF. An internal metadata service runs on port 8888 and returns sensitive credentials.

## Symptoms

Direct requests to `http://127.0.0.1:8888/metadata` through the proxy are correctly blocked. However, a penetration tester has demonstrated that internal metadata can still be extracted through the proxy. The metadata service logs show requests arriving that should have been stopped by the URL validator.

## Bug description

The URL validator in `app/validator.py` maintains a blocklist of hostnames and IP addresses that must not be proxied to. The list includes `127.0.0.1`, `localhost`, `0.0.0.0`, and the AWS metadata IP. It also blocks RFC-1918 private ranges. However the blocklist only covers IPv4 representations of the loopback address. An attacker can use the IPv6 loopback address `[::1]` (or its expanded form `[0:0:0:0:0:0:0:1]`) to reach localhost services, completely bypassing the validator.

## Difficulty

Hard

## Expected turns

15-30
