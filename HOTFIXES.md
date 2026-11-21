# Grim — Operator hotfixes

Internal deploy log. Not the public CHANGELOG and not a `/grim_updates` card.

A hotfix still ships through the same `main` job and tests. It does **not**
bump the version number and does **not** post a public update card. Add the
notes here instead.

## How to mark a deploy as a hotfix

This is the old stealth-updates detector, renamed. Same commit-message label,
now `HOTFIX` instead of `CLASSIFIED`.

1. Put `HOTFIX:`, `[HOTFIX]`, or `this is HOTFIX` in the **merge commit**
   message (squash title counts).
2. Add an entry under **Entries** below. Do not add a public CHANGELOG bullet
   for that ship.
3. Merge to `main` as usual.

Do not infer a hotfix from filenames or feature area. A `CLASSIFIED` commit
label is not a hotfix. Ordinary deploys still bump the version and post public
`/grim_updates` notes with real filenames.

## Entries

_No hotfix ships yet._
