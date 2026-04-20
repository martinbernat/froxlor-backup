#!/bin/bash
# ============================================================
# froxlor-backup - installation script
# Run as root: sudo bash install.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/froxlor-backup"
CONFIG_DIR="/etc/froxlor-backup"
LOG_FILE="/var/log/froxlor-backup.log"
BACKUP_DIR="/var/backups/froxlor-backup"
SSH_KEY="/root/.ssh/froxlor_backup_ed25519"

# ── Helper functions ─────────────────────────────────────────

secure_dir() {
    # secure_dir <path> <owner:group> <perms>
    # Creates directory if it does not exist, always sets owner + perms.
    local path="$1" owner="$2" mode="$3"
    mkdir -p "$path"
    chown "$owner" "$path"
    chmod "$mode"  "$path"
}

secure_file() {
    # secure_file <path> <owner:group> <perms>
    local path="$1" owner="$2" mode="$3"
    chown "$owner" "$path"
    chmod "$mode"  "$path"
}

# ── Main script ──────────────────────────────────────────────

echo "╔══════════════════════════════════════╗"
echo "║  froxlor-backup installation         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run the script as root (sudo bash install.sh)"
    exit 1
fi

# ── 1. Dependencies ──
echo "→ Installing Python dependencies ..."
if command -v pip3 &>/dev/null; then
    pip3 install --quiet PyMySQL PyYAML
elif command -v pip &>/dev/null; then
    pip install --quiet PyMySQL PyYAML
else
    echo "  WARNING: pip not found, install manually: pip3 install PyMySQL PyYAML"
fi

# ── 2. Scripts ──
# Directory readable/executable by root only - scripts do not contain credentials,
# but there is no reason for anyone else to read them.
echo "→ Copying scripts to ${INSTALL_DIR} ..."
secure_dir "$INSTALL_DIR" root:root 700

cp froxlor-backup.py       "$INSTALL_DIR/"
cp froxlor-restore.py      "$INSTALL_DIR/"
cp config.yaml.example     "$INSTALL_DIR/"

secure_file "$INSTALL_DIR/froxlor-backup.py"   root:root 700
secure_file "$INSTALL_DIR/froxlor-restore.py"  root:root 700
secure_file "$INSTALL_DIR/config.yaml.example" root:root 640  # readable by root, group does not need access

# Symlinks in /usr/local/bin - the symlink itself has no own perms,
# security is determined by the target (700 above).
ln -sf "$INSTALL_DIR/froxlor-backup.py"  /usr/local/bin/froxlor-backup
ln -sf "$INSTALL_DIR/froxlor-restore.py" /usr/local/bin/froxlor-restore

# ── 3. Configuration ──
# /etc/froxlor-backup/  →  root:root 700  (other users cannot even see the directory)
# config.yaml           →  root:root 600  (DB + remote credentials - root only)
echo "→ Configuration directory: ${CONFIG_DIR} ..."
secure_dir "$CONFIG_DIR" root:root 700

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    cp config.yaml.example "${CONFIG_DIR}/config.yaml"
    secure_file "${CONFIG_DIR}/config.yaml" root:root 600
    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  IMPORTANT: Edit configuration before running!      │"
    echo "  │  nano ${CONFIG_DIR}/config.yaml                     │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
else
    # On reinstall fix any bad perms - config may have been left with incorrect permissions
    secure_file "${CONFIG_DIR}/config.yaml" root:root 600
    echo "  Configuration already exists, perms fixed (600 root:root)."
fi

# ── 4. Backup directory ──
# 700 root:root - backups may contain sensitive customer data
echo "→ Backup directory: ${BACKUP_DIR} ..."
secure_dir "$BACKUP_DIR" root:root 700

# ── 5. Log file ──
# 600 root:root - logs may contain domain names, paths, error messages with DB names
touch "$LOG_FILE"
secure_file "$LOG_FILE" root:root 600

# ── 6. SSH key for backup server ──
# /root/.ssh must be 700 (SSH will refuse to use it otherwise)
# Private key must be 600 - SSH requires it and will reject 640/644
secure_dir /root/.ssh root:root 700

if [[ ! -f "${SSH_KEY}" ]]; then
    echo "→ Generating SSH key (ed25519) for backup server ..."
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "froxlor-backup@$(hostname -f 2>/dev/null || hostname)"
    # ssh-keygen sets 600 automatically, but explicit for safety:
    secure_file "${SSH_KEY}"       root:root 600
    secure_file "${SSH_KEY}.pub"   root:root 644
    echo ""
    echo "  Copy the public key to the backup server:"
    echo "  ─────────────────────────────────────────────────────"
    cat "${SSH_KEY}.pub"
    echo "  ─────────────────────────────────────────────────────"
    echo "  Command: ssh-copy-id -i ${SSH_KEY} backupuser@backup.server.com"
    echo "  Remember to set in config.yaml: key_file: ${SSH_KEY}"
    echo ""
else
    # Reinstall - fix any bad perms on existing key
    secure_file "${SSH_KEY}"     root:root 600
    secure_file "${SSH_KEY}.pub" root:root 644
    echo "→ SSH key already exists: ${SSH_KEY}  (perms fixed)"
fi

# ── 7. Systemd units ──
# Service + timer files: 644 root:root
# systemd reads them as root, but also read by systemd-analyze running as regular user -
# 644 is the standard for /etc/systemd/system/*.
echo "→ Installing systemd timer ..."
cp froxlor-backup.service /etc/systemd/system/
cp froxlor-backup.timer   /etc/systemd/system/
secure_file /etc/systemd/system/froxlor-backup.service root:root 644
secure_file /etc/systemd/system/froxlor-backup.timer   root:root 644

systemd-analyze verify /etc/systemd/system/froxlor-backup.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable froxlor-backup.timer
echo "  Timer enabled (will run at 02:30 every night)."
echo "  Manual run: systemctl start froxlor-backup.service"

# ── 8. Permissions summary ──
echo ""
echo "→ Configured permissions:"
echo ""
printf "  %-45s %s\n" "File/directory" "owner:group  perms"
printf "  %-45s %s\n" "─────────────────────────────────────────────" "─────────────────────"
for path in \
    "$INSTALL_DIR" \
    "$INSTALL_DIR/froxlor-backup.py" \
    "$INSTALL_DIR/froxlor-restore.py" \
    "$CONFIG_DIR" \
    "${CONFIG_DIR}/config.yaml" \
    "$BACKUP_DIR" \
    "$LOG_FILE" \
    /root/.ssh \
    "${SSH_KEY}" \
    /etc/systemd/system/froxlor-backup.service \
    /etc/systemd/system/froxlor-backup.timer
do
    if [[ -e "$path" ]]; then
        info=$(stat -c "%U:%G  %a" "$path" 2>/dev/null || stat -f "%Su:%Sg  %OLp" "$path" 2>/dev/null || echo "?")
        printf "  %-45s %s\n" "$path" "$info"
    fi
done

# ── Done ──
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Installation complete!                                  ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Next steps:                                             ║"
echo "║  1. Edit configuration:                                  ║"
echo "║     nano /etc/froxlor-backup/config.yaml                 ║"
echo "║  2. Test:    froxlor-backup --dry-run --verbose          ║"
echo "║  3. Run:     froxlor-backup --verbose                    ║"
echo "║  4. Backups: froxlor-restore --list                      ║"
echo "║  5. Restore: froxlor-restore                             ║"
echo "╚══════════════════════════════════════════════════════════╝"
