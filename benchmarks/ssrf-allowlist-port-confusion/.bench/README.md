# ssrf-allowlist-port-confusion

## Difficulty
Hard

## Source
Synthetic — modeled on real-world SSRF bypasses (e.g., Capital One 2019, various HackerOne reports)

## Environment
Python 3.12, Flask, requests library, Alpine Linux

## The bug
The URL validator in `app/validator.py` blocks internal destinations by checking the parsed hostname against a string blocklist (`127.0.0.1`, `localhost`, `0.0.0.0`, `169.254.169.254`) and prefix-matching RFC-1918 ranges. This blocklist only covers IPv4 representations. The IPv6 loopback address `::1` (which `urlparse` extracts from `http://[::1]:8888/`) is not on the list, so requests to `http://[::1]:8888/metadata` pass validation and reach the internal metadata service, leaking credentials.

## Why Hard
- The agent must understand SSRF attack patterns and how URL parsers handle IPv6 literals.
- The string blocklist looks reasonable at first glance; recognizing the IPv6 gap requires security domain knowledge.
- The correct fix should not just add `::1` to the list — it should use `ipaddress.ip_address()` to catch *all* loopback/private representations, including mapped addresses like `::ffff:127.0.0.1`.
- Multiple files are involved (validator, server, internal service) and the agent must trace the data flow to locate the vulnerability.

## Expected fix
Use Python's `ipaddress` module to parse the hostname. If it is a valid IP address, check `.is_loopback`, `.is_private`, `.is_reserved`, or `.is_link_local` instead of relying on a string blocklist. This covers all IPv4, IPv6, and mapped-address forms in one check.

## Pinned at
Synthetic benchmark, not from a real repository
