# Grim development handoff for Cursor

This file carries the practical project context needed when opening Grim in a different editor. It contains no credentials or private conversation history.

## Project

Grim is a Python 3.11+ Discord bot using `discord.py`, xAI, SQLite, and several fixed third-party providers. The production bot runs on a VPS; Cursor is for local editing and development, not hosting.

## Open the project

Clone the GitHub repository in Cursor:

```text
https://github.com/Deathxi/Grim.git
```

Open the cloned repository folder. Install Python 3.11 or newer, then create a local environment and install the locked project dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.

## Project instructions and security references

- `.cursor/rules/grim-project.mdc` — always-on Cursor project and security rules.
- `replit.md` — project overview, commands, features, and operational conventions.
- `OPERATIONS_SECURITY.md` — permissions, HTTP transport, deployment, credential, and incident controls.
- `BACKUP_RESTORE.md` — production runtime data and encrypted backup/restore boundaries.
- `safe_http.py` — centralized outbound HTTP controls. Do not bypass it for arbitrary network destinations.

## Development and verification

Run the non-production checks from the repository root:

```bash
python -m py_compile main.py
python -m unittest tests.test_security_controls
```

Do not start another live bot with the production Discord token. For any local end-to-end Discord testing, use a separate test bot and test server. Keep local credentials in an untracked `.env`; never paste them into Cursor chat, source, tests, or logs.

## Production deployment warning

GitHub Actions deploys every push to `main` to the live VPS by pulling the branch and restarting Grim. This means an ordinary-looking push to `main` changes production. Work on a feature branch, inspect the full diff, run tests, and merge or push to `main` only when the user explicitly approves that production deployment.

The production VPS keeps its own `.env`, service configuration, and runtime state. Persistent context, schedules, chat history, and server settings are stored under `/root/.grim_data` and handled through the encrypted backup process. They are not part of this development checkout and should not be copied here.

## What does not migrate from Replit

The repository transfers code, tests, documentation, and project instructions. Replit chat history, agent sessions, secrets, integrations, and workspace-specific services do not become part of Cursor automatically. Keep secrets in their existing secure stores or configure local development credentials separately; never commit them.

Continue from the current GitHub branch history and this handoff document. No private chat transcript is required for normal code work.
