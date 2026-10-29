# Grim Operations & Security SOP

This guide covers routine administration of the Grim Discord bot. It complements
the encrypted-backup recovery procedure in `BACKUP_RESTORE.md`; it does not
contain credentials or recovery keys.

## 1. Permission model

| Role | What it can do |
| --- | --- |
| Regular member | Use normal conversation, language, reminder, and public utility commands. |
| Moderator with the relevant Discord permission | Manage feeds, welcome messages, live updates, member history, departure logs, and voice actions when Discord grants the required permission. |
| Server owner or Discord administrator | All moderator actions plus moderation word lists, server memories, and the GitHub diagnostic. |
| Creator | Receives creator-aware conversational context, but does **not** bypass Discord permissions, platform rules, safety, or privacy boundaries. |

Management commands must be run inside a server. They fail closed in DMs and
record denied attempts in the audit database without recording message content.
Never elevate someone solely because a display name resembles the creator.

## 2. Routine server setup

1. Confirm Grim has only the Discord permissions it needs in that server:
   read/send messages, message history, embed links, delete messages only if
   moderation is enabled, and voice permissions only if voice use is approved.
2. Have a server owner or administrator configure feeds, welcome messages,
   updates, moderation words, permanent memories, and `/memberlog ENABLE` in the
   intended private staff/mod channel. Grim will reject a departure-log channel
   visible to `@everyone`.
3. Verify each feed and schedule through its status command in the same server.
4. Use `/language preference:<language>` as a member-level preference; `Auto` restores
   message-language matching.
5. Test a normal member path and an administrator path after setup. Do not
   test with secrets or private conversation text.
6. Configure `/grim_updates` in the operations channel to receive deployment
   patch notes and after-reports for unexpected process or Discord connection
   outages. Normal update restarts are excluded. Outage times are approximate
   because an abrupt crash may occur between 30-second heartbeats.
   Process outages require a vanished online heartbeat on the same source
   revision; a revision change is treated as a normal code deployment.

## 3. Routine operations

- Review the bot's service health, Discord permissions, and scheduled feed
  status weekly.
- Confirm the encrypted weekly backup completed and rehearse restoration on a
  non-production copy on the schedule defined in `BACKUP_RESTORE.md`.
- Treat audit records as security telemetry, not conversation archives. They
  retain action, outcome, actor/server IDs, timestamp, and redacted metadata
  for 90 days. Do not export them into public channels.
- Management actions and user-triggered external requests are rate limited.
  If a legitimate action is blocked, wait a minute; do not work around the
  limit by using alternate accounts or commands.
- Use `/members` only in staff contexts. It is an ephemeral, dismissible
  view of member identity and lifecycle data, not a conversation archive.
  Discord does not always provide a verified reason for departure, so treat a
  leave card as a factual record that a member is no longer in the server—not
  proof of a voluntary leave, kick, or ban.

## 4. Safe deployment

1. Run the focused tests and Python compilation check before publishing.
2. Review the diff for accidental runtime data, tokens, private chat content,
   backup keys, or changes to `.github/workflows`.
3. Publish source changes from a trusted workstation/CI through the approved
   GitHub path (`main` → Actions deploy). Never commit from the live VPS.
4. Wait for the deployment workflow to succeed (it must fail if `grim` is not
   active after restart), then check service logs and the bot's normal startup
   behavior.
5. Verify one low-risk command and one administrator-only command in a test
   server. Do not use the live bot to display credentials.

On a fresh VPS, prefer `setup_vps.sh` (creates `/root/.grim_data` mode `0700`,
writes `/root/grim/.env` mode `0600`, creates `/root/grim/venv`, installs
`systemd/grim.service` with sandbox directives). `ExecStart` must use the
venv interpreter (`/root/grim/venv/bin/python3`); deploys install requirements
into that venv, not system `pip3`. Re-running setup reuses an existing GitHub
Actions deploy key instead of appending duplicates. Deploy keys are installed
with `no-pty` / no-forwarding options; the private key is not printed unless
`CONFIRM=print-key` is set for `scripts/print-deploy-key.sh`.

Also set GitHub Actions secret `VPS_SSH_FINGERPRINT` to the Droplet host-key
fingerprint (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) so deploys
reject MITM. Leave it empty only until that value is recorded.

### Live-host write policy
- The live VPS must **never** push Grim source, `version.txt`, or runtime JSON
  into `Deathxi/Grim`. There is no supported source-sync script; do not recreate
  `push_to_github.py` or Contents:Write auto-sync from `main.py`.
- Runtime state lives only under `~/.grim_data` and is recovered via encrypted
  backups to the separate private backup repository (see `BACKUP_RESTORE.md`).
- Optional `GITHUB_PERSONAL_ACCESS_TOKEN` in `/root/grim/.env` is **read-only**
  for `/grim_updates` commit metadata. Prefer a fine-grained token with
  Contents: Read on `Deathxi/Grim` only. Never put the backup-repo write token
  in Grim's `.env`.

## 5. Credential rotation

Rotate a credential immediately after suspected exposure, personnel changes,
or an unexpected GitHub/Discord/xAI failure:

1. Create a replacement credential with the minimum required scope.
2. Update it only through the platform secret manager or the VPS's protected
   environment configuration; never paste it into Discord, commits, issues,
   logs, or this document.
3. Restart the relevant service and run the non-sensitive health check.
4. Revoke the old credential after the replacement is confirmed working.
5. Record the incident and actions taken in the operator's private incident
   record, without copying the credential.

## 6. Incident response

For suspected unauthorized management activity, credential disclosure,
unexpected deletions, or repeated failures:

1. Preserve evidence: note the time, server, command, and visible error;
   avoid copying private member messages.
2. Disable or remove the affected bot permission/token where appropriate.
3. Review recent redacted audit records and Discord's server audit log.
4. Rotate affected credentials and deploy a fix before restoring access.
5. Restore runtime state only from a verified encrypted backup, following
   `BACKUP_RESTORE.md`.
6. Verify server-scoped settings after recovery before re-enabling schedules
   or moderation.

## 7. Shutdown and recovery

For planned maintenance, stop the bot cleanly before changing its runtime
directory or making a backup. For an unplanned interruption, do not overwrite
existing state; take a copy, check the service logs, then restore the most
recent verified backup if needed. Atomic JSON writes reduce partial-file risk,
but they are not a substitute for the encrypted backup and restore rehearsal.