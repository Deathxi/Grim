#!/usr/bin/env python3
"""Create and upload an encrypted backup of Grim's persistent runtime data.

This script deliberately has no dependency on Grim's Discord process.  It only
reads the data directory, encrypts a temporary tarball with OpenSSL, and
uploads the encrypted bytes to a separate GitHub repository.
"""

import argparse
import base64
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DATA_DIR = os.path.expanduser("~/.grim_data")
DEFAULT_PASSPHRASE_FILE = "/root/.config/grim-backup/passphrase"
DEFAULT_RETENTION = 8
GITHUB_API = "https://api.github.com"
GITHUB_BRANCH = "main"
ARCHIVE_PREFIX = "grim-data-"
ARCHIVE_SUFFIX = ".tar.gz.enc"
MAX_GITHUB_FILE_SIZE = 90 * 1024 * 1024

# Runtime data is intentionally permissive so new learned-state files are
# included automatically.  Names that commonly contain credentials are never
# copied, even if someone accidentally creates them inside ~/.grim_data.
EXCLUDED_BASENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*credential*",
    "*secret*",
    "*token*",
    "*password*",
)


class BackupError(RuntimeError):
    """An expected, user-actionable backup failure."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BackupError(f"{name} is not configured")
    return value


def _validate_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BackupError(
            "GRIM_BACKUP_GITHUB_REPO must be a GitHub owner/repository name"
        )
    return repo


def _read_passphrase(path: str) -> bytes:
    passphrase_path = Path(path).expanduser()
    try:
        passphrase = passphrase_path.read_bytes().strip()
    except OSError as exc:
        raise BackupError(
            f"could not read backup passphrase file {passphrase_path}: {exc}"
        ) from exc
    if not passphrase:
        raise BackupError(f"backup passphrase file {passphrase_path} is empty")
    return passphrase


def _is_excluded(path: Path) -> bool:
    basename = path.name.lower()
    return any(
        fnmatch.fnmatchcase(basename, pattern)
        for pattern in EXCLUDED_BASENAME_PATTERNS
    )


def _runtime_files(data_dir: Path):
    """Yield regular, non-sensitive files below data_dir."""
    for root, dirs, files in os.walk(data_dir, followlinks=False):
        root_path = Path(root)
        # Never descend through a symlink that happens to be under the data
        # directory; it could point outside the intended backup scope.
        dirs[:] = [
            directory
            for directory in dirs
            if not (root_path / directory).is_symlink()
            and not _is_excluded(root_path / directory)
        ]
        for filename in files:
            path = root_path / filename
            if path.is_symlink() or not path.is_file() or _is_excluded(path):
                continue
            yield path


def create_archive(data_dir: str, output_path: str) -> int:
    """Write a gzip-compressed tar archive and return the file count."""
    source = Path(data_dir).expanduser().resolve()
    if not source.is_dir():
        raise BackupError(f"runtime data directory does not exist: {source}")

    files = list(_runtime_files(source))
    with tarfile.open(output_path, mode="w:gz") as archive:
        for path in files:
            # Relative names make restore into a freshly-created
            # ~/.grim_data directory safe and predictable.
            archive.add(path, arcname=path.relative_to(source), recursive=False)
    return len(files)


def encrypt_archive(input_path: str, output_path: str, passphrase: bytes) -> None:
    """Encrypt a tarball with OpenSSL without exposing the passphrase in argv."""
    if shutil.which("openssl") is None:
        raise BackupError("openssl is required but was not found on PATH")

    command = [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-salt",
        "-pbkdf2",
        "-iter",
        "600000",
        "-md",
        "sha256",
        "-pass",
        "stdin",
        "-in",
        input_path,
        "-out",
        output_path,
    ]
    result = subprocess.run(
        command,
        input=passphrase + b"\n",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise BackupError(f"OpenSSL encryption failed: {detail or 'unknown error'}")


def _github_request(
    method: str, repo: str, token: str, endpoint: str, payload=None, accept=None
):
    url = f"{GITHUB_API}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept or "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "Grim-Backup",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read()
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        try:
            details = json.loads(exc.read()).get("message", "unknown GitHub error")
        except (ValueError, TypeError):
            details = "unknown GitHub error"
        raise BackupError(
            f"GitHub API {method} {endpoint} failed with HTTP {exc.code}: {details}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BackupError(f"could not reach GitHub: {exc.reason}") from exc


def upload_archive(repo: str, token: str, archive_path: Path, archive_name: str):
    if archive_path.stat().st_size > MAX_GITHUB_FILE_SIZE:
        raise BackupError(
            f"encrypted archive is larger than the GitHub Contents API limit "
            f"({MAX_GITHUB_FILE_SIZE // (1024 * 1024)} MiB)"
        )
    content = base64.b64encode(archive_path.read_bytes()).decode("ascii")
    endpoint = (
        f"/repos/{repo}/contents/archives/"
        f"{urllib.parse.quote(archive_name, safe='')}"
    )
    result = _github_request(
        "PUT",
        repo,
        token,
        endpoint,
        {
            "message": f"Weekly Grim data backup: {archive_name}",
            "content": content,
            "branch": GITHUB_BRANCH,
        },
    )
    if not result.get("content"):
        raise BackupError("GitHub did not confirm the backup upload")


def _backup_names(repo: str, token: str):
    names = []
    page = 1
    while True:
        endpoint = (
            f"/repos/{repo}/contents/archives?ref={GITHUB_BRANCH}"
            f"&per_page=100&page={page}"
        )
        listing = _github_request("GET", repo, token, endpoint)
        if not isinstance(listing, list):
            raise BackupError("GitHub archives path is not a directory")
        names.extend(
            item["name"]
            for item in listing
            if item.get("type") == "file"
            and item.get("name", "").startswith(ARCHIVE_PREFIX)
            and item.get("name", "").endswith(ARCHIVE_SUFFIX)
        )
        if len(listing) < 100:
            break
        page += 1
    return sorted(names, reverse=True)


def prune_old_archives(repo: str, token: str, retention: int) -> int:
    """Keep the newest retention archives and delete older backup files."""
    names = _backup_names(repo, token)
    removed = 0
    for name in names[retention:]:
        endpoint = (
            f"/repos/{repo}/contents/archives/"
            f"{urllib.parse.quote(name, safe='')}"
        )
        existing = _github_request("GET", repo, token, endpoint)
        sha = existing.get("sha")
        if not sha:
            raise BackupError(f"GitHub did not return a SHA for old archive {name}")
        _github_request(
            "DELETE",
            repo,
            token,
            endpoint,
            {"message": f"Remove expired Grim backup: {name}", "sha": sha},
        )
        removed += 1
    return removed


def perform_backup(args) -> None:
    data_dir = args.data_dir or os.environ.get("GRIM_BACKUP_DATA_DIR", DEFAULT_DATA_DIR)
    passphrase_file = (
        args.passphrase_file
        or os.environ.get("GRIM_BACKUP_PASSPHRASE_FILE", DEFAULT_PASSPHRASE_FILE)
    )
    passphrase = _read_passphrase(passphrase_file)

    retention_value = os.environ.get("GRIM_BACKUP_RETENTION", str(DEFAULT_RETENTION))
    try:
        retention = int(retention_value)
    except ValueError as exc:
        raise BackupError("GRIM_BACKUP_RETENTION must be a positive integer") from exc
    if retention < 1:
        raise BackupError("GRIM_BACKUP_RETENTION must be at least 1")

    repo = token = None
    if not args.dry_run:
        repo = _validate_repo(_required_env("GRIM_BACKUP_GITHUB_REPO"))
        token = _required_env("GRIM_BACKUP_GITHUB_TOKEN")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"{ARCHIVE_PREFIX}{timestamp}-{uuid.uuid4().hex[:8]}{ARCHIVE_SUFFIX}"
    keep_path = Path(args.output).expanduser() if args.output else None

    with tempfile.TemporaryDirectory(prefix="grim-backup-") as temp_dir:
        compressed_path = Path(temp_dir) / "grim-data.tar.gz"
        encrypted_path = Path(temp_dir) / archive_name
        count = create_archive(data_dir, str(compressed_path))
        encrypt_archive(str(compressed_path), str(encrypted_path), passphrase)

        if keep_path:
            keep_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(encrypted_path, keep_path)

        if args.dry_run:
            print(
                f"Dry run succeeded: encrypted {count} runtime file(s) "
                f"({encrypted_path.stat().st_size} bytes)"
            )
            return

        upload_archive(repo, token, encrypted_path, archive_name)
        removed = prune_old_archives(repo, token, retention)

    print(
        f"Backup uploaded: {archive_name} "
        f"({count} runtime file(s)); removed {removed} expired archive(s)"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Encrypt and upload Grim's ~/.grim_data runtime state."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="create and encrypt an archive without contacting GitHub",
    )
    parser.add_argument(
        "--data-dir",
        help="override the runtime data directory (normally ~/.grim_data)",
    )
    parser.add_argument(
        "--passphrase-file",
        help="override the root-only passphrase file path",
    )
    parser.add_argument(
        "--output",
        help="copy the encrypted archive to this path (useful for local validation)",
    )
    return parser.parse_args()


def main() -> int:
    try:
        perform_backup(parse_args())
    except BackupError as exc:
        print(f"ERROR: Grim backup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # The systemd journal should show unexpected failures too, while
        # avoiding a traceback that could accidentally include configuration.
        print(f"ERROR: Grim backup failed unexpectedly: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())