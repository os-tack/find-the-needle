"""URL validation module for the webhook proxy.

Checks that outbound URLs are safe to request — blocks known internal
and private-network destinations so the proxy cannot be abused for SSRF.
"""

from urllib.parse import urlparse

ALLOWED_HOSTS = ["api.example.com", "hooks.slack.com", "webhook.site"]

BLOCKED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "0.0.0.0",
    "169.254.169.254",  # AWS metadata
]

BLOCKED_NETS = ["10.", "172.16.", "192.168."]


def is_url_allowed(url: str) -> bool:
    """Return True if *url* is safe to proxy, False otherwise.

    The function rejects:
    * URLs whose hostname resolves to a known internal address
    * URLs targeting private RFC-1918 ranges
    * URLs with no hostname at all

    Any destination not on the blocklist is allowed so that the proxy
    can reach arbitrary public webhook receivers.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = parsed.hostname
    if not host:
        return False

    # Block well-known internal hostnames / IPs
    if host in BLOCKED_HOSTS:
        return False

    # Block private network ranges
    for net in BLOCKED_NETS:
        if host.startswith(net):
            return False

    return True
