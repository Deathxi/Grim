# Grim backup and restore

Grim's learned server context, chat history, digests, schedules, and settings
are stored in `/root/.grim_data` on the VPS. The weekly backup service creates
an OpenSSL-encrypted archive and uploads it to the separate private repository
configured in `/etc/grim-backup.env`. The repository token is used only by the
backup service; it is not the token Grim uses for code deployment.

The service keeps the newest eight archives in `archives/`. It briefly stops
Grim before reading the data directory and restarts it afterward, including
when a backup fails. This avoids archiving a partially-written JSON file or an
in-progress SQLite update. It never backs up `/root/grim/.env`, provider
credentials, or files with credential-like names inside the data directory.
The passphrase file is never uploaded.

The latest sanitized live rehearsal record is
[`docs/backup-rehearsal-2026-09-03.md`](docs/backup-rehearsal-2026-09-03.md).

## Initial setup

Create a private repository dedicated to backups, for example
`your-account/grim-private-backups`. Create a fine-grained GitHub token with
**Contents: Read and write** access to that repository only. Do not reuse
Grim's code-repository token. Choose a long, unique recovery passphrase and
store it in an offline password manager or printed recovery record.

Run `setup_vps.sh` on the VPS and provide the backup repository, dedicated
token, and passphrase when prompted. The installer stores the token in
`/etc/grim-backup.env` and the passphrase in
`/root/.config/grim-backup/passphrase`, both mode `0600`, then enables the
weekly timer.

Check the installation:

```bash
systemctl is-enabled grim-backup.timer
systemctl status grim-backup.timer --no-pager
systemctl list-timers grim-backup.timer --no-pager
```

Run a test backup immediately after setup. This briefly pauses Grim, uploads
one archive, and is the recommended end-to-end check:

```bash
systemctl start grim-backup.service
journalctl -u grim-backup.service -n 50 --no-pager
```

The service exits non-zero on missing configuration, encryption errors,
network/API errors, or retention errors. These failures are visible in the
journal and do not stop the Discord service.

## Restore on a replacement VPS

Do not start Grim until the data has been restored and checked.

1. Rebuild the VPS with `setup_vps.sh`, or install Python, OpenSSL, and the
   Grim repository manually. Recover the **offline backup passphrase** and the
   dedicated backup-repository token. Provider credentials are intentionally
   not included in these archives and must be configured separately in
   `/root/grim/.env`.
2. Set temporary shell variables without committing or printing them:

   ```bash
   export GRIM_BACKUP_GITHUB_REPO='your-account/grim-private-backups'
   export GRIM_BACKUP_GITHUB_TOKEN='use-the-dedicated-fine-grained-token'
   export ARCHIVE_NAME='grim-data-YYYYMMDDTHHMMSSZ-xxxxxxxx.tar.gz.enc'
   ```

3. List the retained archives and choose the newest known-good filename:

   ```bash
   curl -fsSL \
     -H "Authorization: Bearer ${GRIM_BACKUP_GITHUB_TOKEN}" \
     -H "Accept: application/vnd.github+json" \
     "https://api.github.com/repos/${GRIM_BACKUP_GITHUB_REPO}/contents/archives?ref=main" \
     | python3 -c 'import json,sys; print("\n".join(x["name"] for x in json.load(sys.stdin) if x.get("name","").endswith(".tar.gz.enc")))'
   ```

4. Download the selected encrypted archive. The raw Contents API response
   requires the same repository-scoped token:

   ```bash
   curl -fsSL \
     -H "Authorization: Bearer ${GRIM_BACKUP_GITHUB_TOKEN}" \
     -H "Accept: application/vnd.github.raw" \
     "https://api.github.com/repos/${GRIM_BACKUP_GITHUB_REPO}/contents/archives/${ARCHIVE_NAME}?ref=main" \
     -o "/tmp/${ARCHIVE_NAME}"
   ```

5. Write the recovered passphrase to the root-only file expected by the
   service. Do not put it in the repository or in a shell command history:

   ```bash
   install -d -m 700 /root/.config/grim-backup
   read -r -s -p 'Backup passphrase: ' RECOVERY_PASSPHRASE
   printf '\n'
   printf '%s' "${RECOVERY_PASSPHRASE}" > /root/.config/grim-backup/passphrase
   unset RECOVERY_PASSPHRASE
   chmod 600 /root/.config/grim-backup/passphrase
   ```

6. Decrypt and inspect the archive before changing live data. The encryption
   parameters must match the backup utility:

   ```bash
   openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 \
     -pass file:/root/.config/grim-backup/passphrase \
     -in "/tmp/${ARCHIVE_NAME}" -out /tmp/grim-data.tar.gz
   tar -tzf /tmp/grim-data.tar.gz
   ```

   The listing should contain only relative runtime-data paths such as
   `chat_history.db`, digest files, and schedule files. It must not contain
   `.env` or provider credentials. If decryption or the listing fails, stop
   and try another archive or verify the offline passphrase.

7. Stop Grim, preserve any newly-created directory, and extract the archive
   into a fresh data directory:

   ```bash
   systemctl stop grim
   if [ -d /root/.grim_data ]; then
     mv /root/.grim_data "/root/.grim_data.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
   fi
   install -d -m 700 /root/.grim_data
   tar --no-same-owner -xzf /tmp/grim-data.tar.gz -C /root/.grim_data
   chown -R root:root /root/.grim_data
   ```

8. Validate the restored data before starting Grim:

   ```bash
   find /root/.grim_data -maxdepth 1 -type f -printf '%f\n' | sort
   python3 - <<'PY'
   import json
   import sqlite3
   from pathlib import Path

   data = Path("/root/.grim_data")
   for path in sorted(data.rglob("*.json")):
       json.loads(path.read_text())
       print(f"Valid JSON: {path.relative_to(data)}")
   db = data / "chat_history.db"
   if db.exists():
       with sqlite3.connect(db) as conn:
           result = conn.execute("PRAGMA integrity_check").fetchone()[0]
       if result != "ok":
           raise RuntimeError(f"SQLite integrity check failed: {result}")
       print("SQLite integrity check: ok")
   print("Restored Grim runtime data passed basic validation")
   PY
   ```

9. Restore `/root/grim/.env` separately from your offline provider-secret
   records, verify its mode is `0600`, then start and observe Grim:

   ```bash
   chmod 600 /root/grim/.env
   systemctl start grim
   systemctl status grim --no-pager
   journalctl -u grim -n 100 --no-pager
   ```

After Grim is healthy, run `systemctl start grim-backup.service` once to
confirm the replacement VPS can create and upload a new encrypted archive.