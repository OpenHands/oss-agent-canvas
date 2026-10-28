import argparse
import io
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from urllib.request import ProxyHandler, Request, build_opener


def request(path, body=None):
    url = os.environ["REVIEW_GATEWAY_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Bearer " + os.environ["REVIEW_GATEWAY_TOKEN"],
        "Content-Type": "application/json",
    }
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, data=data, headers=headers), timeout=40) as response:
        value = response.read(64_000_001)
        if len(value) > 64_000_000:
            raise ValueError("Gateway response exceeds the byte limit")
        return value


def checkout(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.name not in (".git", ".openhands") for p in directory.iterdir()):
        raise ValueError("Checkout destination is not empty")
    with tarfile.open(fileobj=io.BytesIO(request("/checkout")), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > 250_000_000:
            raise ValueError("Repository archive exceeds its size limit")
        roots = {
            PurePosixPath(m.name).parts[0]
            for m in members
            if PurePosixPath(m.name).parts
        }
        if len(roots) != 1:
            raise ValueError("Repository archive has multiple roots")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if (
                not parts
                or PurePosixPath(member.name).is_absolute()
                or ".." in parts
                or ".git" in parts
            ):
                raise ValueError("Unsafe repository archive path")
            if len(parts) < 2 or not member.isfile():
                continue
            target = directory.joinpath(*parts[1:])
            if target.is_symlink() or any(
                parent.is_symlink()
                for parent in target.parents
                if parent != directory.parent
            ):
                raise ValueError("Repository archive crosses a symlink")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    print("Prepared the requested commit's source tree.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("get", "submit", "checkout"))
    parser.add_argument("value")
    args = parser.parse_args()
    if args.operation == "checkout":
        checkout(args.value)
    elif args.operation == "get":
        print(request("/github" + args.value).decode())
    else:
        data = Path(args.value).read_bytes()
        if len(data) > 64000:
            raise ValueError("Proposal exceeds the byte limit")
        print(request("/proposal", json.loads(data)).decode())
