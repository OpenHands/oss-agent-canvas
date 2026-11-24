import base64
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath


class RepositoryFiles:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.archive = None
        self.members = {}
        self.index = {
            "tree": [
                {"path": name, "type": "blob"} for name in snapshot.get("files", {})
            ]
        }
        if snapshot.get("archive_path"):
            for field, filename in (
                ("archive", "repository.tar.gz"),
                ("index", "repository-index.json"),
            ):
                path = Path(snapshot[field + "_path"])
                if not path.exists():
                    path = Path("/tmp/replay") / filename
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != snapshot[field + "_sha256"]
                ):
                    raise ValueError("Pinned repository artifact changed")
                if field == "archive":
                    self.archive = tarfile.open(path)
                    self.members = {
                        str(
                            PurePosixPath(*PurePosixPath(member.name).parts[1:])
                        ): member
                        for member in self.archive.getmembers()
                        if len(PurePosixPath(member.name).parts) > 1 and member.isfile()
                    }
                else:
                    self.index = json.loads(path.read_text())

    def read(self, path):
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("Unsafe repository read")
        if self.archive:
            if path not in self.members:
                raise KeyError(path)
            return self.archive.extractfile(self.members[path]).read()
        return self.snapshot["files"][path].encode()

    def contents(self, path=""):
        known = {entry["path"]: entry for entry in self.index["tree"]}
        if path in known:
            entry = known[path]
            if entry["type"] not in ("blob", "tree"):
                return 404, {"message": "Endpoint not present in frozen scenario"}
            if entry["type"] == "blob":
                if self.archive and path not in self.members:
                    return 404, {"message": "Endpoint not present in frozen scenario"}
                data = self.read(path)
                return 200, {
                    "path": path,
                    "name": PurePosixPath(path).name,
                    "type": "file",
                    "sha": entry.get("sha"),
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                    "size": len(data),
                }
        prefix = path.rstrip("/") + "/" if path else ""
        children = {}
        for name, entry in known.items():
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix) :]
            child = tail.split("/", 1)[0]
            children[child] = {
                "name": child,
                "path": prefix + child,
                "type": "dir" if "/" in tail else "file",
                "sha": known.get(prefix + child, {}).get("sha"),
            }
        if children or not path:
            return 200, list(children.values())
        if not self.archive:
            return 404, {"message": "Endpoint not present in frozen scenario"}
        return 404, {"message": "Not Found"}


def materialize(case, root):
    import os
    import shutil
    import subprocess

    repository = RepositoryFiles(case["repository_snapshot"])
    for entry in root.iterdir():
        shutil.rmtree(
            entry
        ) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    names = repository.members if repository.archive else repository.snapshot["files"]
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ValueError("Unsafe repository archive path")
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(repository.read(name))
        if repository.archive and repository.members[name].mode & 0o111:
            target.chmod(0o700)
    environment = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-10-29T00:00:00Z",
        GIT_COMMITTER_DATE="2026-10-29T00:00:00Z",
    )
    for argv in (
        ["git", "init", "-q", "--initial-branch=main"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Replay",
            "-c",
            "user.email=replay@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Pinned repository snapshot",
        ],
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/" + case["native_payload"]["repository"] + ".git",
        ],
    ):
        subprocess.run(argv, cwd=root, env=environment, check=True, capture_output=True)
