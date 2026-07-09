#!/usr/bin/env python3
"""Unit tests for run_matrix_v41.resolve_host_ostk — the explicit host bench
harness resolver (never bare PATH `ostk`). The Jul-9 smoke ran a stale Jul-2
dev build from PATH that predated the arm/binary receipts; these tests pin the
fail-closed contract: $OSTK_BENCH_BIN override, else exactly one hash-verified
frozen-bin/ostk-host-<sha256> pin, no PATH fallback, corrupt/ambiguous pins
refuse loudly."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_matrix_v41 as rm


def _pin(dirpath: Path, content: bytes, name: str | None = None) -> Path:
    sha = hashlib.sha256(content).hexdigest()
    p = dirpath / (name or f"ostk-host-{sha}")
    p.write_bytes(content)
    return p


class TestResolveHostOstk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.frozen = Path(self._tmp.name) / "frozen-bin"
        self.frozen.mkdir()
        self._orig_frozen = rm.FROZEN_BIN
        rm.FROZEN_BIN = self.frozen
        rm._HOST_OSTK = None  # reset the per-process cache
        os.environ.pop("OSTK_BENCH_BIN", None)

    def tearDown(self):
        rm.FROZEN_BIN = self._orig_frozen
        rm._HOST_OSTK = None
        os.environ.pop("OSTK_BENCH_BIN", None)
        self._tmp.cleanup()

    def test_single_verified_pin_resolves(self):
        content = b"fake-host-ostk-with-requested_arm"
        pin = _pin(self.frozen, content)
        path, sha = rm.resolve_host_ostk()
        self.assertEqual(path, pin)
        self.assertEqual(sha, hashlib.sha256(content).hexdigest())
        # cached: second call returns the same tuple without re-hashing
        self.assertEqual(rm.resolve_host_ostk(), (path, sha))

    def test_no_pin_fails_closed_never_path(self):
        with self.assertRaises(SystemExit) as cm:
            rm.resolve_host_ostk()
        msg = str(cm.exception)
        self.assertIn("refusing to fall back to PATH", msg)

    def test_corrupt_pin_fails_closed(self):
        _pin(self.frozen, b"real content",
             name="ostk-host-" + "0" * 64)  # filename claims a wrong hash
        with self.assertRaises(SystemExit) as cm:
            rm.resolve_host_ostk()
        self.assertIn("HOST PIN CORRUPT", str(cm.exception))

    def test_multiple_pins_are_ambiguous(self):
        _pin(self.frozen, b"pin one")
        _pin(self.frozen, b"pin two")
        with self.assertRaises(SystemExit) as cm:
            rm.resolve_host_ostk()
        self.assertIn("exactly one must glob", str(cm.exception))

    def test_env_override_wins_over_pin(self):
        _pin(self.frozen, b"pin content")
        override = Path(self._tmp.name) / "dev-ostk"
        override.write_bytes(b"dev build")
        os.environ["OSTK_BENCH_BIN"] = str(override)
        path, sha = rm.resolve_host_ostk()
        self.assertEqual(path, override)
        self.assertEqual(sha, hashlib.sha256(b"dev build").hexdigest())

    def test_env_override_missing_file_fails(self):
        os.environ["OSTK_BENCH_BIN"] = str(Path(self._tmp.name) / "nope")
        with self.assertRaises(SystemExit) as cm:
            rm.resolve_host_ostk()
        self.assertIn("not a file", str(cm.exception))

    def test_run_cell_dispatch_uses_resolved_binary(self):
        """The cell cmd must start with the resolved harness path — a bare
        'ostk' argv[0] is exactly the stale-PATH defect this fix removes."""
        content = b"fake-host-ostk"
        pin = _pin(self.frozen, content)
        path, _sha = rm.resolve_host_ostk()
        self.assertEqual(path, pin)
        # dry-run returns before any subprocess: safe to exercise run_cell
        status, _ = rm.run_cell("m", "encoding-mojibake", "native", dry_run=True)
        self.assertEqual(status, "success")


if __name__ == "__main__":
    unittest.main()
