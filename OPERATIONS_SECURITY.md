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
   updates, moderation words, permanent memories, and `/grim_memberlog` in the
   intended private staff/mod channel.
3. Verify each feed and schedule through its status command in the same server.
4. Use `/grim_language` as a member-level preference; `Auto` restores
   message-language matching.
5. Test a normal member path and an administrator path after setup. Do not
   test with secrets or private conversation text.

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
- Use `/grim_members` only in staff contexts. It is an ephemeral, dismissible
  view of member identity and lifecycle data, not a conversation archive.
  Discord does not always provide a verified reason for departure, so treat a
  leave card as a factual record that a member is no longer in the server—not
  proof of a voluntary leave, kick, or ban.

## 4. Safe deployment

1. Run the focused tests and Python compilation check before publishing.
2. Review the diff for accidental runtime data, tokens, private chat content,
   backup keys, or changes to `.github/workflows`.
3. Publish source changes through the approved GitHub deployment path.
4. Wait for the deployment workflow to succeed, then check service logs and
   the bot's normal startup behavior.
5. Verify one low-risk command and one administrator-only command in a test
   server. Do not use the live bot to display credentials.

The live VPS must not write source changes back to GitHub. Runtime state is
preserved through its data directory and encrypted backups, avoiding
deployment loops and unnecessary token exposure.

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