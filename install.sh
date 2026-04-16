#!/bin/bash
# ============================================================
# froxlor-backup – inštalačný skript
# Spusti ako root: sudo bash install.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/froxlor-backup"
CONFIG_DIR="/etc/froxlor-backup"
LOG_FILE="/var/log/froxlor-backup.log"
BACKUP_DIR="/var/backups/froxlor-backup"
SSH_KEY="/root/.ssh/froxlor_backup_ed25519"

# ── Pomocné funkcie ──────────────────────────────────────────

secure_dir() {
    # secure_dir <cesta> <owner:group> <perms>
    # Vytvorí adresár ak neexistuje, vždy nastaví owner + perms.
    local path="$1" owner="$2" mode="$3"
    mkdir -p "$path"
    chown "$owner" "$path"
    chmod "$mode"  "$path"
}

secure_file() {
    # secure_file <cesta> <owner:group> <perms>
    local path="$1" owner="$2" mode="$3"
    chown "$owner" "$path"
    chmod "$mode"  "$path"
}

# ── Hlavný skript ────────────────────────────────────────────

echo "╔══════════════════════════════════════╗"
echo "║  froxlor-backup inštalácia           ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Kontrola root
if [[ $EUID -ne 0 ]]; then
    echo "CHYBA: Spusti skript ako root (sudo bash install.sh)"
    exit 1
fi

# ── 1. Závislosti ──
echo "→ Inštalácia Python závislostí ..."
if command -v pip3 &>/dev/null; then
    pip3 install --quiet PyMySQL PyYAML
elif command -v pip &>/dev/null; then
    pip install --quiet PyMySQL PyYAML
else
    echo "  VAROVANIE: pip nenájdený, nainštaluj ručne: pip3 install PyMySQL PyYAML"
fi

# ── 2. Skripty ──
# Adresár čitateľný/spustiteľný len rootom – skripty neobsahujú credentials,
# ale nie je dôvod aby ich čítal ktokoľvek iný.
echo "→ Kopírujem skripty do ${INSTALL_DIR} ..."
secure_dir "$INSTALL_DIR" root:root 700

cp froxlor-backup.py       "$INSTALL_DIR/"
cp froxlor-restore.py      "$INSTALL_DIR/"
cp config.yaml.example     "$INSTALL_DIR/"

secure_file "$INSTALL_DIR/froxlor-backup.py"   root:root 700
secure_file "$INSTALL_DIR/froxlor-restore.py"  root:root 700
secure_file "$INSTALL_DIR/config.yaml.example" root:root 640  # čitateľný root-om, skupina nepotrebuje

# Symlinky v /usr/local/bin – samotný symlink nemá vlastné perms,
# bezpečnosť je daná targetom (700 vyššie).
ln -sf "$INSTALL_DIR/froxlor-backup.py"  /usr/local/bin/froxlor-backup
ln -sf "$INSTALL_DIR/froxlor-restore.py" /usr/local/bin/froxlor-restore

# ── 3. Konfigurácia ──
# /etc/froxlor-backup/  →  root:root 700  (iní používatelia adresár ani nevidia)
# config.yaml           →  root:root 600  (credentials pre DB + remote – len root)
echo "→ Konfiguračný adresár: ${CONFIG_DIR} ..."
secure_dir "$CONFIG_DIR" root:root 700

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    cp config.yaml.example "${CONFIG_DIR}/config.yaml"
    secure_file "${CONFIG_DIR}/config.yaml" root:root 600
    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  DÔLEŽITÉ: Uprav konfiguráciu pred spustením!       │"
    echo "  │  nano ${CONFIG_DIR}/config.yaml                     │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
else
    # Aj pri reinštalácii oprav perms – config mohol zostať so zlými právami
    secure_file "${CONFIG_DIR}/config.yaml" root:root 600
    echo "  Konfigurácia už existuje, perms opravené (600 root:root)."
fi

# ── 4. Zálohovací adresár ──
# 700 root:root – zálohy môžu obsahovať citlivé dáta zákazníkov
echo "→ Zálohovací adresár: ${BACKUP_DIR} ..."
secure_dir "$BACKUP_DIR" root:root 700

# ── 5. Log súbor ──
# 600 root:root – logy môžu obsahovať názvy domén, cesty, chybové hlášky s DB menami
touch "$LOG_FILE"
secure_file "$LOG_FILE" root:root 600

# ── 6. SSH kľúč pre backup server ──
# /root/.ssh musí byť 700 (SSH to inak odmietne použiť)
# Privátny kľúč musí byť 600 – SSH to vyžaduje a odmietne 640/644
secure_dir /root/.ssh root:root 700

if [[ ! -f "${SSH_KEY}" ]]; then
    echo "→ Generujem SSH kľúč (ed25519) pre backup server ..."
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "froxlor-backup@$(hostname -f 2>/dev/null || hostname)"
    # ssh-keygen nastavuje 600 automaticky, ale explicitne pre istotu:
    secure_file "${SSH_KEY}"       root:root 600
    secure_file "${SSH_KEY}.pub"   root:root 644
    echo ""
    echo "  Skopíruj verejný kľúč na backup server:"
    echo "  ─────────────────────────────────────────────────────"
    cat "${SSH_KEY}.pub"
    echo "  ─────────────────────────────────────────────────────"
    echo "  Príkaz: ssh-copy-id -i ${SSH_KEY} backupuser@backup.server.com"
    echo "  Nezabudni v config.yaml nastaviť: key_file: ${SSH_KEY}"
    echo ""
else
    # Reinštalácia – oprav prípadné zlé perms existujúceho kľúča
    secure_file "${SSH_KEY}"     root:root 600
    secure_file "${SSH_KEY}.pub" root:root 644
    echo "→ SSH kľúč už existuje: ${SSH_KEY}  (perms opravené)"
fi

# ── 7. Systemd jednotky ──
# Service + timer súbory: 644 root:root
# systemd ich číta ako root, ale číta ich aj systemd-analyze bežiaci ako bežný user –
# 644 je štandard pre /etc/systemd/system/*.
echo "→ Inštalácia systemd timera ..."
cp froxlor-backup.service /etc/systemd/system/
cp froxlor-backup.timer   /etc/systemd/system/
secure_file /etc/systemd/system/froxlor-backup.service root:root 644
secure_file /etc/systemd/system/froxlor-backup.timer   root:root 644

systemd-analyze verify /etc/systemd/system/froxlor-backup.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable froxlor-backup.timer
echo "  Timer zapnutý (spustí sa o 02:30 každú noc)."
echo "  Manuálne spustenie: systemctl start froxlor-backup.service"

# ── 8. Výpis súhrnu permissions ──
echo ""
echo "→ Nastavené permissions:"
echo ""
printf "  %-45s %s\n" "Súbor/adresár" "owner:group  perms"
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

# ── Hotovo ──
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Inštalácia dokončená!                                   ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Nasledujúce kroky:                                      ║"
echo "║  1. Uprav konfiguráciu:                                  ║"
echo "║     nano /etc/froxlor-backup/config.yaml                 ║"
echo "║  2. Testuj:  froxlor-backup --dry-run --verbose          ║"
echo "║  3. Spusti:  froxlor-backup --verbose                    ║"
echo "║  4. Zálohy:  froxlor-restore --list                      ║"
echo "║  5. Obnova:  froxlor-restore                             ║"
echo "╚══════════════════════════════════════════════════════════╝"
