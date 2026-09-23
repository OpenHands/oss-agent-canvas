import unittest
from datetime import datetime, timedelta, timezone

from main import Release, current_versions, replace_all_exact, replace_exact, select_release


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class ReleaseSelectionTests(unittest.TestCase):
    def release(self, version: str, age: timedelta) -> Release:
        return Release(version, NOW - age, f"https://example.test/{version}", {})

    def test_release_must_be_at_least_168_hours_old(self):
        releases = [
            self.release("1.2.0", timedelta(days=6, hours=23, minutes=59)),
            self.release("1.1.0", timedelta(days=7)),
        ]
        self.assertEqual(select_release(releases, NOW).version, "1.1.0")

    def test_selects_newest_eligible_release_not_newest_overall(self):
        releases = [
            self.release("2.0.0", timedelta(days=2)),
            self.release("1.10.0", timedelta(days=8)),
            self.release("1.9.9", timedelta(days=20)),
        ]
        self.assertEqual(select_release(releases, NOW).version, "1.10.0")

    def test_preserves_requested_major(self):
        releases = [
            self.release("25.0.0", timedelta(days=20)),
            self.release("24.9.1", timedelta(days=8)),
        ]
        self.assertEqual(select_release(releases, NOW, major=24).version, "24.9.1")


class ReplacementTests(unittest.TestCase):
    def test_exact_replacement_requires_one_occurrence(self):
        self.assertEqual(replace_exact("a=1", "1", "2"), "a=2")
        with self.assertRaises(RuntimeError):
            replace_exact("a=1 b=1", "1", "2")

    def test_all_replacement_requires_expected_count(self):
        self.assertEqual(replace_all_exact("v1 v1", "v1", "v2", 2), "v2 v2")
        with self.assertRaises(RuntimeError):
            replace_all_exact("v1", "v1", "v2", 2)

    def test_parses_all_supported_sdk_pins(self):
        text = """
FROM python:3.13.15-slim-trixie AS python-runtime
FROM node:24.21.0-trixie-slim AS node-runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
NODE_TARBALL=/tmp/node-v22.19.0-linux-${NARCH}.tar.xz
https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-${NARCH}.tar.xz
ARG RELEASE_TAG="openvscode-server-v1.98.2"
https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-${TARCH}
"""
        self.assertEqual(
            current_versions(text),
            {
                "python": "3.13.15",
                "node": "24.21.0",
                "uv": "0.11.6",
                "acp_node": "22.19.0",
                "openvscode": "1.98.2",
                "tini": "0.19.0",
            },
        )


if __name__ == "__main__":
    unittest.main()
