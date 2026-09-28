import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from main import (
    Release,
    current_versions,
    replace_all_exact,
    replace_exact,
    select_release,
    update_sdk_dockerfile,
)


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


class DockerfileUpdateTests(unittest.TestCase):
    @patch("main.urllib.request.urlopen")
    @patch("main.python_releases", return_value=[])
    @patch("main.github_releases")
    @patch("main.node_releases")
    def test_acp_node_updates_download_directory_and_filename(
        self, mock_node_releases, mock_github_releases, _mock_python, mock_urlopen
    ):
        mock_node_releases.return_value = [
            Release("22.20.0", NOW - timedelta(days=8), "https://node.test", {})
        ]
        mock_github_releases.return_value = []
        mock_urlopen.return_value.read.return_value = (
            ("a" * 64 + "  node-v22.20.0-linux-x64.tar.xz\n")
            + ("b" * 64 + "  node-v22.20.0-linux-arm64.tar.xz\n")
        ).encode()
        text = """
FROM python:3.13.15-slim-trixie AS python-runtime
FROM node:24.21.0-trixie-slim AS node-runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
x86_64|amd64) NARCH=x64; NODE_SHA256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc;;
aarch64|arm64) NARCH=arm64; NODE_SHA256=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd;;
NODE_TARBALL="/tmp/node-v22.19.0-linux-${NARCH}.tar.xz"
https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-${NARCH}.tar.xz
ARG RELEASE_TAG="openvscode-server-v1.98.2"
https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-${TARCH}
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
"""
        updated, changes = update_sdk_dockerfile(text, "token", NOW)
        self.assertIn("/dist/v22.20.0/node-v22.20.0-linux-${NARCH}.tar.xz", updated)
        self.assertNotIn("v22.19.0", updated)
        self.assertEqual(changes[0]["name"], "acp_node")


if __name__ == "__main__":
    unittest.main()
