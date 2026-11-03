import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "plugins/oss-unmanaged-dependency-update/scripts/main.py"


def load_module():
    spec = importlib.util.spec_from_file_location("unmanaged", str(MAIN))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = load_module()
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

# Mirrors the real SDK Dockerfile after the CVE-2026-111497 fix: the ACP
# providers block pins Node 24 (fetched with curl, `x86_64|amd64` arch case)
# and the OpenVSCode block pins a second, unrelated Node 22 (fetched with
# wget, `x64` arch case).
DOCKERFILE = """
FROM python:3.13.15-slim-trixie AS python-runtime
FROM node:24.23.0-trixie-slim AS node-runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
RUN set -eux; \\
    ARCH=$(uname -m); \\
    case "$ARCH" in \\
      x86_64|amd64) NARCH=x64; NODE_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;; \\
      aarch64|arm64) NARCH=arm64; NODE_SHA256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb;; \\
      *) echo "Unsupported architecture for ACP providers: '$ARCH'" >&2; exit 1;; \\
    esac; \\
    NODE_TARBALL="/tmp/node-v24.22.1-linux-${NARCH}.tar.xz"; \\
    curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused \\
        "https://nodejs.org/dist/v24.22.1/node-v24.22.1-linux-${NARCH}.tar.xz" -o "$NODE_TARBALL"; \\
    echo "$NODE_SHA256  $NODE_TARBALL" | sha256sum -c -;
RUN set -eux; \\
    case "${arch}" in \\
      x64) NARCH=x64; NODE_SHA256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc;; \\
      arm64) NARCH=arm64; NODE_SHA256=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd;; \\
      armhf) NARCH=armv7l; NODE_SHA256=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee;; \\
      *) echo "Unsupported architecture for OpenVSCode Node: '${arch}'" >&2; exit 1;; \\
    esac; \\
    NODE_TARBALL="/tmp/node-v22.23.4-linux-${NARCH}.tar.xz"; \\
    wget -nv -O "$NODE_TARBALL" "https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-${NARCH}.tar.xz"; \\
    echo "$NODE_SHA256  $NODE_TARBALL" | sha256sum -c -;
ARG RELEASE_TAG="openvscode-server-v1.98.2"
https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-${TARCH}
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/
"""

SHASUMS = (
    "1111111111111111111111111111111111111111111111111111111111111111  node-v24.23.0-linux-x64.tar.xz\n"
    "2222222222222222222222222222222222222222222222222222222222222222  node-v24.23.0-linux-arm64.tar.xz\n"
)


class CurrentVersionsTests(unittest.TestCase):
    def test_two_node_blocks_do_not_break_the_acp_parse(self):
        # Before the fix the bare `node-v…-linux-${NARCH}` pattern matched both
        # blocks (24.22.1 and 22.23.4), tripping the "exactly one" guard and
        # failing the run with "Expected one acp_node version".
        versions = main.current_versions(DOCKERFILE)
        self.assertEqual(versions["acp_node"], "24.22.1")
        self.assertEqual(versions["node"], "24.23.0")
        self.assertEqual(versions["openvscode"], "1.98.2")
        self.assertEqual(versions["tini"], "0.19.0")


class UpdateTests(unittest.TestCase):
    def _bump(self, text):
        releases = [
            main.Release(
                "24.23.0",
                NOW - timedelta(days=8),
                "https://node.test/v24.23.0",
                {},
            )
        ]
        with patch.object(main, "python_releases", return_value=[]), patch.object(
            main, "node_releases", return_value=releases
        ), patch.object(main, "github_releases", return_value=[]), patch.object(
            main, "http_json", return_value={}
        ), patch.object(
            main.urllib.request, "urlopen"
        ) as urlopen:
            urlopen.return_value.read.return_value = SHASUMS.encode()
            return main.update_sdk_dockerfile(text, "token", NOW)

    def test_bump_updates_acp_block_only(self):
        updated, changes = self._bump(DOCKERFILE)
        acp = updated.split('case "${arch}" in')[0]
        editor = updated.split('case "${arch}" in')[1]

        self.assertEqual([c["name"] for c in changes], ["acp_node"])
        self.assertIn("/dist/v24.23.0/node-v24.23.0-linux-${NARCH}.tar.xz", acp)
        self.assertNotIn("v24.22.1", acp)
        self.assertIn("x86_64|amd64) NARCH=x64; NODE_SHA256=" + "1" * 64, acp)
        self.assertIn("aarch64|arm64) NARCH=arm64; NODE_SHA256=" + "2" * 64, acp)

        # The editor block's Node 22 pin and its own checksums are untouched.
        self.assertIn("node-v22.23.4-linux-${NARCH}.tar.xz", editor)
        self.assertIn("x64) NARCH=x64; NODE_SHA256=" + "c" * 64, editor)
        self.assertIn("arm64) NARCH=arm64; NODE_SHA256=" + "d" * 64, editor)


if __name__ == "__main__":
    unittest.main()
