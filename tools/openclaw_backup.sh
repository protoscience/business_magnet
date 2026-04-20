#!/usr/bin/env bash
# Back up OpenClaw state from the VPS to the local PC.
# Runs on a systemd timer; hashes the remote config each tick and only
# creates a new archive when something changed. Retains MAX_BACKUPS copies.
set -euo pipefail

VPS_HOST="${OPENCLAW_BACKUP_VPS:-admin@minions.protoscience.org}"
BACKUP_DIR="${OPENCLAW_BACKUP_DIR:-$HOME/.openclaw-backups}"
MAX_BACKUPS="${OPENCLAW_BACKUP_KEEP:-5}"

mkdir -p "$BACKUP_DIR"
HASH_FILE="$BACKUP_DIR/.last_hash"

current_hash=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS_HOST" \
    'sha256sum ~/.openclaw/openclaw.json 2>/dev/null' | awk '{print $1}')
if [[ -z "$current_hash" ]]; then
    echo "openclaw-backup: could not read remote config hash" >&2
    exit 1
fi

last_hash=""
[[ -f "$HASH_FILE" ]] && last_hash=$(cat "$HASH_FILE")

archive_count=$(find "$BACKUP_DIR" -maxdepth 1 -name 'openclaw_backups_*.tar.gz' | wc -l)

if [[ "$current_hash" == "$last_hash" && "$archive_count" -gt 0 ]]; then
    exit 0  # no change, nothing to do
fi

ts=$(date +%Y%m%d%H%M%S)
name="openclaw_backups_${ts}.tar.gz"
remote_path="/tmp/$name"
local_path="$BACKUP_DIR/$name"

ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS_HOST" \
    "openclaw backup create --output $remote_path" > /dev/null
scp -o ConnectTimeout=10 "$VPS_HOST:$remote_path" "$local_path"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS_HOST" "rm -f $remote_path"

echo "$current_hash" > "$HASH_FILE"

cd "$BACKUP_DIR"
# shellcheck disable=SC2012  # we need -t ordering, not find
ls -1t openclaw_backups_*.tar.gz | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm --

echo "openclaw-backup: wrote $local_path"
