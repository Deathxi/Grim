import os
import base64
import hashlib
import json
import urllib.request
import urllib.error

TOKEN = os.environ.get("GRIM_GITHUB_SYNC_TOKEN")
REPO = "Deathxi/Grim"
BRANCH = "main"
FILES = [
    "main.py",
    "CHANGELOG.md",
    ".gitignore",
    ".replit",
    "replit.md",
    "requirements.txt",
    "push_to_github.py",
    "backup_grim_data.py",
    "run_grim_backup.sh",
    "setup_vps.sh",
    "BACKUP_RESTORE.md",
    "scripts/post-merge.sh",
    "systemd/grim-backup.service",
    "systemd/grim-backup.timer",
]
# NOTE: version.txt and updates_data.json are runtime data managed exclusively by the deployed bot.
# Never push them from this script — the dev environment has stale copies that would corrupt the real data.

MAIN_HASH_FILE = os.path.expanduser("~/.grim_data/main_hash.txt")

def api(method, path, data=None):
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "GrimBot"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def get_stored_hash():
    try:
        with open(MAIN_HASH_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def store_hash(h):
    os.makedirs(os.path.dirname(MAIN_HASH_FILE), exist_ok=True)
    with open(MAIN_HASH_FILE, "w") as f:
        f.write(h)

if not TOKEN:
    raise SystemExit(
        "Missing GRIM_GITHUB_SYNC_TOKEN. Add a GitHub classic personal access token "
        "with the repo scope through Replit Secrets."
    )
else:
    # Collect files that exist locally
    to_push = [f for f in FILES if os.path.exists(f)]
    skipped = [f for f in FILES if not os.path.exists(f)]

    if skipped:
        for f in skipped:
            print(f"  SKIP  {f} (not found locally)")

    if not to_push:
        print("Nothing to push.")
    else:
        print(f"Pushing {len(to_push)} file(s) to github.com/{REPO} as a single commit...\n")

        # Get current HEAD commit SHA and tree SHA
        ref = api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
        if "object" not in ref:
            raise SystemExit(
                f"Unable to access {REPO}/{BRANCH}: {ref.get('message', 'unknown GitHub error')}"
            )
        head_sha = ref["object"]["sha"]
        base_tree_sha = api("GET", f"/repos/{REPO}/git/commits/{head_sha}")["tree"]["sha"]

        # Build tree entries
        tree = []
        for filepath in to_push:
            with open(filepath, "rb") as f:
                content = f.read()
            blob = api("POST", f"/repos/{REPO}/git/blobs", {
                "content": base64.b64encode(content).decode(),
                "encoding": "base64"
            })
            tree.append({
                "path": filepath,
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"]
            })
            print(f"  OK    {filepath}")

        # Create new tree
        new_tree = api("POST", f"/repos/{REPO}/git/trees", {
            "base_tree": base_tree_sha,
            "tree": tree
        })

        # Create commit
        new_commit = api("POST", f"/repos/{REPO}/git/commits", {
            "message": "Deploy Grim",
            "tree": new_tree["sha"],
            "parents": [head_sha]
        })

        # Update branch ref
        api("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}", {
            "sha": new_commit["sha"],
            "force": False
        })

        # Update stored main.py hash if it was pushed
        if "main.py" in to_push:
            store_hash(md5("main.py"))

        print(f"\nDone. Single commit: {new_commit['sha'][:7]}")
