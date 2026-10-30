import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


class GitHTTP:
    def __init__(self, source, directory, world):
        self.directory, self.world = Path(directory), world
        self.bare = self.directory / "fixture/review.git"
        shutil.copytree(Path(source) / ".git", self.bare)
        self.git("config", "core.bare", "true")
        self.git("symbolic-ref", "HEAD", "refs/heads/fix-add")

    def git(self, *args):
        return subprocess.run(
            ["git", "--git-dir", str(self.bare), *args],
            capture_output=True,
            check=True,
            timeout=10,
        )

    def request(self, method, target, raw, headers):
        url = urlsplit(target)
        if (
            method not in ("GET", "POST")
            or (method == "POST" and not url.path.endswith("/git-upload-pack"))
            or "git-receive-pack" in target
        ):
            return 403, {"Content-Type": "text/plain"}, b"Read-only Git fixture"
        self.git("update-ref", "refs/heads/fix-add", self.world.pr["head"]["sha"])
        self.git("update-ref", "refs/heads/main", self.world.pr["base"]["sha"])
        self.git("update-ref", "refs/pull/7/head", self.world.pr["head"]["sha"])
        env = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(self.directory),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": url.path,
            "REQUEST_METHOD": method,
            "QUERY_STRING": url.query,
            "CONTENT_TYPE": headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(raw)),
            "HTTP_GIT_PROTOCOL": headers.get("Git-Protocol", ""),
            "REMOTE_ADDR": "127.0.0.1",
        }
        result = subprocess.run(
            ["git", "http-backend"], input=raw, capture_output=True, env=env, timeout=20
        )
        header, separator, data = result.stdout.partition(b"\r\n\r\n")
        if result.returncode or not separator:
            raise RuntimeError("Git fixture backend failed")
        values = dict(line.decode().split(": ", 1) for line in header.split(b"\r\n"))
        status = int(values.pop("Status", "200 OK").split()[0])
        self.world.requests.append(
            {
                "method": method,
                "path": url.path,
                "read_only": True,
                "status": status,
                "body_bytes": len(raw),
                "response_bytes": len(data),
                "response_sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        return status, values, data
