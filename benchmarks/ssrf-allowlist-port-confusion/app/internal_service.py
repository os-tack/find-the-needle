"""Simulated internal metadata service.

Represents a sensitive backend (think AWS instance-metadata, Consul,
Vault, or an internal admin API) that should never be reachable from
the public-facing webhook proxy.
"""

import json
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


class MetadataHandler(BaseHTTPRequestHandler):
    """Serves fake-but-sensitive metadata on every path."""

    def do_GET(self):
        if self.path == "/metadata":
            payload = {
                "secret": "INTERNAL_SECRET_12345",
                "role": "admin",
                "instance_id": "i-0abc123def456",
                "account": "123456789012",
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    # Silence per-request log lines
    def log_message(self, format, *args):
        pass


class HTTPServerV6(HTTPServer):
    """HTTPServer subclass that binds to IPv6."""
    address_family = socket.AF_INET6


def main():
    # We need both IPv4 and IPv6 listeners so that requests to
    # 127.0.0.1:8888 AND [::1]:8888 both reach this service.
    v4 = HTTPServer(("0.0.0.0", 8888), MetadataHandler)
    print("Internal metadata service listening on 0.0.0.0:8888 (IPv4)", flush=True)

    try:
        v6 = HTTPServerV6(("::1", 8888), MetadataHandler)
        print("Internal metadata service listening on [::1]:8888 (IPv6)", flush=True)
        threading.Thread(target=v6.serve_forever, daemon=True).start()
    except OSError:
        # IPv6 may not be available in some environments
        print("Warning: IPv6 listener failed, only IPv4 available", flush=True)

    v4.serve_forever()


if __name__ == "__main__":
    main()
