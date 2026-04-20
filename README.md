# froxlor-backup

Automated backup system for the **Froxlor** hosting panel.  
Backs up web files, mailboxes, MySQL/MariaDB databases, and logs **per-domain / per-customer**, transfers backups to a remote server, and provides an **interactive restore wizard**.

---

## Contents

1. [Context - what Froxlor offers natively](#1-context--what-froxlor-offers-natively)
2. [Solution architecture](#2-solution-architecture)
3. [Project files](#3-project-files)
4. [Requirements](#4-requirements)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Backup - froxlor-backup](#7-backup--froxlor-backup)
8. [Restore - froxlor-restore](#8-restore--froxlor-restore)
9. [Backup directory structure](#9-backup-directory-structure)
10. [Retention](#10-retention)
11. [Remote storage](#11-remote-storage)
12. [Consistency on a live server](#12-consistency-on-a-live-server)
13. [Permissions](#13-permissions)
14. [Automation - systemd timer](#14-automation--systemd-timer)
15. [Notifications](#15-notifications)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Context - what Froxlor offers natively

Froxlor has a built-in **DataDump** (called "Backup" prior to version 2.1.0) accessible via `customer_extras.php?page=export`. It is a **manual on-demand export**, not an automated backup system.

| Feature | DataDump (native) | froxlor-backup (this project) |
|---|---|---|
| Trigger | Manually by customer | Automatically via systemd timer |
| Granularity | Per-customer | Per-domain + per-customer (DB) |
| Backup destination | Customer docroot | External server (SSH/S3/SFTP/…) |
| Retention | None | Daily / weekly / monthly |
| Panel-based restore | No | CLI wizard (`froxlor-restore`) |
| Logging | No | Optionally yes |
| Encryption | GPG (optional) | Native target encryption (e.g. S3 SSE) |

The Froxlor documentation for DataDump explicitly states:
> *"Regular backups and/or snapshots are the responsibility of the admin."*

---

## 2. Solution architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Froxlor server                                             │
│                                                             │
│  systemd timer (02:30 every night)                          │
│       │                                                     │
│       ▼                                                     │
│  froxlor-backup.py                                          │
│       │                                                     │
│       ├── reads Froxlor MySQL DB (panel_domains,            │
│       │   panel_customers, mail_users, panel_databases)     │
│       │                                                     │
│       ├── per domain:                                       │
│       │     web.tar.gz      ← documentroot                  │
│       │     mail/*.tar.gz   ← maildir per mailbox           │
│       │     logs.tar.gz     ← access/error logs (opt.)      │
│       │                                                     │
│       ├── per customer (once):                              │
│       │     databases/*.sql.gz  ← mysqldump of each DB      │
│       │                                                     │
│       ├── manifest.json  (backup metadata)                  │
│       │                                                     │
│       ├── retention (removes old local backups)             │
│       │                                                     │
│       └── transfer to remote(s)                             │
│             rsync+SSH  or  rclone (S3/SFTP/B2/…)            │
│                                                             │
│  froxlor-restore.py                                         │
│       └── interactive wizard (web / mail / db / all)        │
└─────────────────────────────────────────────────────────────┘
```

### Froxlor DB tables used by the backup script

| Table | Purpose |
|---|---|
| `panel_domains` | List of domains, `documentroot`, `isemaildomain` |
| `panel_customers` | Customers, `loginname`, `guid` (uid/gid), `documentroot` |
| `mail_users` | Mailboxes - `homedir`, `maildir`, `domainid` |
| `panel_databases` | Customer databases - `databasename`, `dbserver` |

---

## 3. Project files

```
froxlor-backup/
├── froxlor-backup.py       - backup engine
├── froxlor-restore.py      - interactive restore wizard
├── config.yaml.example     - example configuration
├── froxlor-backup.service  - systemd service unit
├── froxlor-backup.timer    - systemd timer (02:30am daily)
├── install.sh              - installation script
└── README.md               - this documentation
```

After installation:

```
/opt/froxlor-backup/            - scripts (root:root 700)
/etc/froxlor-backup/config.yaml - configuration (root:root 600)
/var/backups/froxlor-backup/    - local backups (root:root 700)
/var/log/froxlor-backup.log     - log file (root:root 600)
/root/.ssh/froxlor_backup_ed25519  - SSH key for remote (root:root 600)
/usr/local/bin/froxlor-backup   - symlink
/usr/local/bin/froxlor-restore  - symlink
```

---

## 4. Requirements

### System packages

```bash
# Debian / Ubuntu
apt install python3 python3-pip tar gzip rsync openssh-client

# For rclone (S3 / Backblaze / SFTP / ...)
curl https://rclone.org/install.sh | bash

# MySQL / MariaDB client (for mysqldump)
apt install mariadb-client   # or mysql-client
```

### Python packages

```bash
pip3 install PyMySQL PyYAML
```

---

## 5. Installation

```bash
# 1. Clone / extract the project
git clone <repo-url> /tmp/froxlor-backup
cd /tmp/froxlor-backup

# 2. Run the installer (requires root)
sudo bash install.sh
```

The installer:
- Installs Python dependencies
- Copies scripts to `/opt/froxlor-backup/` with permissions `root:root 700`
- Creates `/etc/froxlor-backup/config.yaml` from the template (if it does not exist) with `root:root 600`
- Creates the backup directory `/var/backups/froxlor-backup/` with `root:root 700`
- Generates the SSH key `/root/.ssh/froxlor_backup_ed25519` (if it does not exist)
- Installs and enables the systemd timer
- Prints a permissions table for review

```
# 3. Edit configuration
nano /etc/froxlor-backup/config.yaml

# 4. Test backup (no writes to disk)
froxlor-backup --dry-run --verbose

# 5. First real backup
froxlor-backup --verbose
```

---

## 6. Configuration

Configuration file: `/etc/froxlor-backup/config.yaml`

### froxlor_db

Froxlor database user (read-only access is sufficient). Credentials can be found in `/var/www/froxlor/lib/userdata.inc.php` in the `$sql` array.

```yaml
froxlor_db:
  host: localhost
  port: 3306
  socket: null          # alternative: /var/run/mysqld/mysqld.sock
  user: froxlor
  password: "password"
  name: froxlor
```

### db_root_servers

Root access to MySQL servers for `mysqldump`. Froxlor supports multiple DB servers — the index must match the `dbserver` ID in the `panel_databases` table. Credentials can be found in `userdata.inc.php` in the `$sql_root` array.

```yaml
db_root_servers:
  0:                    # dbserver=0 (default, single server)
    host: localhost
    port: 3306
    socket: null
    user: root
    password: "root_password"
  1:                    # second DB server (if present)
    host: db2.internal
    port: 3306
    user: root
    password: "password2"
```

### froxlor_paths

Must match the settings in Froxlor Admin → Settings → System → Paths.

```yaml
froxlor_paths:
  document_root_prefix: /var/customers/webs   # system.documentroot_prefix
  mail_home_dir: /var/customers/mail           # system.vmail_homedir
  logs_dir: /var/customers/logs                # system.logfiles_directory
```

### backup

```yaml
backup:
  web: true        # files from each domain's documentroot
  mail: true       # mailboxes (per domain via mail_users.domainid)
  databases: true  # mysqldump of all customer databases
  logs: false      # access/error logs (usually not needed)
```

### Domain / customer filtering

```yaml
exclude_domains: ["test.example.com", "dev.example.com"]
exclude_customers: ["web_test"]
include_only_customers: []    # if non-empty, backs up ONLY these customers
```

### remotes

You can define multiple targets — all with `enabled: true` will be used.

```yaml
remotes:
  - name: backup-ssh
    type: rsync_ssh
    enabled: true
    host: backup.example.com
    port: 22
    user: backupuser
    key_file: /root/.ssh/froxlor_backup_ed25519
    path: /mnt/backups/froxlor
    rsync_extra_args: ["--bwlimit=50000"]   # throttle 50 MB/s

  - name: s3-wasabi
    type: rclone
    enabled: false
    rclone_remote: wasabi                   # name from `rclone config`
    path: my-bucket/froxlor-backups
    rclone_config: /root/.config/rclone/rclone.conf
    rclone_extra_args: ["--transfers=4"]
```

### retention

```yaml
retention:
  daily: 7      # last 7 daily backups
  weekly: 4     # 1 backup/week, last 4 weeks
  monthly: 6    # 1 backup/month, last 6 months
```

Retention is applied to the **local directory**. Since rsync uses `--delete`, locally deleted backups are automatically deleted on the remote server as well.

---

## 7. Backup - froxlor-backup

### Usage

```bash
# Back up all domains
froxlor-backup

# Single domain only
froxlor-backup --domain example.com

# Single customer only (all their domains + DBs)
froxlor-backup --customer web1

# Dry-run - only prints what would be backed up
froxlor-backup --dry-run --verbose

# Without transfer to remote (local backup only)
froxlor-backup --skip-transfer

# Without applying retention
froxlor-backup --skip-retention
```

### All flags

| Flag | Description |
|---|---|
| `--config PATH` | Path to config file (default: `/etc/froxlor-backup/config.yaml`) |
| `--domain NAME` | Back up this domain only |
| `--customer NAME` | Back up this customer only (loginname) |
| `--skip-transfer` | Do not transfer to remote |
| `--skip-retention` | Do not apply retention policy |
| `--dry-run` | Simulation - no writes |
| `--verbose / -v` | Verbose logging (DEBUG level) |

### Backup contents

**Per domain:**

```
example.com/2026-04-16/
├── web/
│   └── web.tar.gz          ← full domain documentroot
├── mail/
│   ├── info@example.com.tar.gz
│   └── admin@example.com.tar.gz
├── logs/
│   └── logs.tar.gz         ← only if backup.logs: true
└── manifest.json
```

**Per customer (databases):**

```
_db_web1/2026-04-16/
├── databases/
│   ├── web1_wp.sql.gz
│   └── web1_eshop.sql.gz
└── manifest.json
```

### manifest.json

Each backup contains metadata:

```json
{
  "version": 2,
  "tool": "froxlor-backup 1.0.0",
  "timestamp": "2026-04-16T02:31:47.123456",
  "domain": "example.com",
  "customer": "web1",
  "customerid": 5,
  "domain_id": 12,
  "contents": ["web", "mail"]
}
```

---

## 8. Restore - froxlor-restore

### Interactive wizard

```bash
froxlor-restore
```

Steps through 4 prompts:

```
Step 1 - select entity:
  1)  [domain]     example.com        (last backup: 2026-04-16, 12 total)
  2)  [domain]     shop.example.com   (last backup: 2026-04-16,  7 total)
  3)  [databases]  customer web1      (last backup: 2026-04-16,  7 total)

Step 2 - select date:
  1)  2026-04-16  (today)      [web, mail]
  2)  2026-04-15  (yesterday)  [web, mail]

Step 3 - what to restore:
  1)  web files (documentroot)
  2)  email mailboxes
  3)  databases (SQL dump)
  4)  all available

Step 4 - confirmation:
  Entity : example.com
  Date   : 2026-04-15
  Type   : web files (documentroot)
  Proceed with restore? [y/N]:
```

### Non-interactive usage

```bash
# Restore web files for a domain from a specific date
froxlor-restore --domain example.com --date 2026-04-15 --type web

# Restore customer databases
froxlor-restore --domain web1 --type db

# Dry-run (no writes)
froxlor-restore --domain example.com --type all --dry-run

# List available backups
froxlor-restore --list
```

### Restore types

| Type | What it restores | Safety |
|---|---|---|
| `web` | Extracts `web.tar.gz` into `documentroot` | Creates a pre-restore snapshot of the current state as `docroot.pre_restore_TIMESTAMP` |
| `mail` | Extracts maildir archives into `homedir` | Select a specific mailbox or all at once |
| `db` / `databases` | Imports `.sql.gz` dumps via `mysql` client | Confirmation required for each DB individually |
| `all` | Web + mail (+ DB if targeting a customer) | Combination of the above |

### List available backups

```bash
froxlor-restore --list
```

```
Available backups in /var/backups/froxlor-backup:

Domain                                        Backups (newest → oldest)
────────────────────────────────────────────────────────────────────────
  example.com                                 2026-04-16  [web, mail]  (7 backups)
  shop.example.com                            2026-04-16  [web]        (7 backups)

Customer (databases)                          Last backup
────────────────────────────────────────────────────────────────────────
  web1                                        2026-04-16  [databases]  (7 backups)
```

---

## 9. Backup directory structure

### Local directory (`/var/backups/froxlor-backup/`)

```
/var/backups/froxlor-backup/
│
├── example.com/                    ← one domain
│   ├── 2026-04-16/                 ← backup date (ISO 8601)
│   │   ├── web/web.tar.gz
│   │   ├── mail/info@example.com.tar.gz
│   │   ├── mail/admin@example.com.tar.gz
│   │   └── manifest.json
│   ├── 2026-04-15/
│   └── 2026-04-09/                 ← weekly backup
│
├── shop.example.com/
│   └── 2026-04-16/
│       ├── web/web.tar.gz
│       └── manifest.json
│
└── _db_web1/                       ← databases for customer web1
    ├── 2026-04-16/
    │   ├── databases/web1_wp.sql.gz
    │   ├── databases/web1_eshop.sql.gz
    │   └── manifest.json
    └── 2026-04-01/                 ← monthly backup
```

### Remote server (identical structure)

Rsync synchronises the entire local directory to the remote, including deletions — the structure is identical. Same applies to rclone.

---

## 10. Retention

The algorithm retains backups across three levels simultaneously:

```
Example with daily:7, weekly:4, monthly:6
(backups every day for 2 months)

April 2026:
  16 (today)  ← daily
  15          ← daily
  14          ← daily
  13          ← daily
  12          ← daily
  11          ← daily
  10          ← daily  + weekly (week 15)
   9          ← DELETED
   8          ← DELETED
   7          ← DELETED
   6          ← DELETED
   5          ← DELETED
   4          ← DELETED
   3          ← weekly (week 14)
  ...

February 2026:
   1          ← monthly

November 2025: DELETED (older than 6 months)
```

Retention is applied to the local directory. Rsync with `--delete` then removes the same backups on the remote as well.

---

## 11. Remote storage

### rsync + SSH

The simplest option. Requires SSH access to the backup server.

```bash
# On the backup server - create user and directory
adduser --disabled-password backupuser
mkdir -p /mnt/backups/froxlor
chown backupuser:backupuser /mnt/backups/froxlor

# Copy the public key (generated by install.sh)
cat /root/.ssh/froxlor_backup_ed25519.pub
# → paste into /home/backupuser/.ssh/authorized_keys on the backup server
```

In `config.yaml`:

```yaml
remotes:
  - name: backup-ssh
    type: rsync_ssh
    enabled: true
    host: backup.example.com
    port: 22
    user: backupuser
    key_file: /root/.ssh/froxlor_backup_ed25519
    path: /mnt/backups/froxlor
```

### rclone (S3 / Wasabi / Backblaze B2 / SFTP / …)

```bash
# Configure rclone (interactive)
rclone config

# Test
rclone ls wasabi:my-bucket
```

In `config.yaml`:

```yaml
remotes:
  - name: s3-wasabi
    type: rclone
    enabled: true
    rclone_remote: wasabi
    path: my-bucket/froxlor-backups
```

### Multiple remotes simultaneously

```yaml
remotes:
  - name: primary-ssh
    type: rsync_ssh
    enabled: true
    ...
  - name: offsite-s3
    type: rclone
    enabled: true
    ...
```

Both are used — the backup is transferred to both destinations.

---

## 12. Consistency on a live server

### MySQL / MariaDB - InnoDB

The script uses `mysqldump --single-transaction --lock-tables=false`:

```
START TRANSACTION WITH CONSISTENT SNAPSHOT
  → REPEATABLE READ isolation level
  → no table locks
  → active writes continue uninterrupted
  → dump is consistent as of the moment it started
COMMIT
```

**InnoDB** (standard in all modern web applications — WordPress, Joomla, PrestaShop, …): **fully consistent**.

### MySQL / MariaDB - MyISAM

MyISAM does not support transactions. `--lock-tables=false` disables automatic locking, but the dump **may not be consistent** if writes are in progress. MyISAM is a legacy engine — most web hosting applications have not used it since ~2015.

### Dovecot / Maildir

Dovecot uses the **Maildir** format. Each email is a separate file, delivered **atomically**:

```
1. Dovecot writes the message to  tmp/1234567890.hostname
2. atomic rename()             →  new/1234567890.hostname
```

A tar backup will see each file either fully or not at all — a partially written email cannot occur. If the rename happens during tar, the email simply will not be in the backup (tar returns exit code 1, which the script correctly ignores — `returncode not in (0, 1)` → error, `== 1` → OK).

**Dovecot index files** (`.index`, `.index.log`, `.index.cache`) may be backed up in an inconsistent state. This is not critical — Dovecot automatically rebuilds them on first access after a restore.

The `--warning=no-file-changed` flag in the tar command suppresses warnings for these files and avoids flooding the log with noise.

### Consistency summary

| Data type | Technique | Consistent? | Note |
|---|---|---|---|
| InnoDB tables | `--single-transaction` | **Yes** | No locks |
| MyISAM tables | `--lock-tables=false` | Partial | Legacy engine, rarely seen today |
| Maildir files | tar + ignore exit 1 | **Yes** | Atomic delivery |
| Dovecot indexes | tar + `--warning=no-file-changed` | No | Dovecot rebuilds automatically |
| Web files | tar | **Yes** (static) | Risk only for files written by the application |

---

## 13. Permissions

The installer configures the following permissions:

| File / directory | owner:group | perms | Reason |
|---|---|---|---|
| `/opt/froxlor-backup/` | root:root | `700` | Directory invisible to other users |
| `froxlor-backup.py` | root:root | `700` | Executable by root only |
| `froxlor-restore.py` | root:root | `700` | Executable by root only |
| `config.yaml.example` | root:root | `640` | Template without sensitive data |
| `/etc/froxlor-backup/` | root:root | `700` | Directory invisible to other users |
| **`config.yaml`** | root:root | **`600`** | Contains DB root password and SSH key path |
| `/var/backups/froxlor-backup/` | root:root | `700` | Backups contain customer data |
| `/var/log/froxlor-backup.log` | root:root | `600` | Logs may contain DB names and paths |
| `/root/.ssh/` | root:root | `700` | SSH requirement — rejected otherwise |
| `froxlor_backup_ed25519` | root:root | `600` | SSH requirement — rejects `640`/`644` |
| `froxlor_backup_ed25519.pub` | root:root | `644` | Public key |
| `froxlor-backup.service` | root:root | `644` | systemd reads as root |
| `froxlor-backup.timer` | root:root | `644` | systemd reads as root |

> **Note on symlinks:** `/usr/local/bin/froxlor-backup` and `/usr/local/bin/froxlor-restore` are symlinks — the symlink's own permissions are irrelevant; security is determined by the target (`700`).

On **reinstall** (re-running `install.sh`) permissions are corrected on all existing files — including `config.yaml` if someone accidentally changed its permissions.

---

## 14. Automation - systemd timer

### How it works

```
froxlor-backup.timer  →  triggers  →  froxlor-backup.service
```

The timer runs every night at **02:30** with a random delay of 0–15 minutes (`RandomizedDelaySec=900`) — this prevents collisions on servers running multiple backup processes.

`Persistent=true` means that if the server was off at 02:30, the timer will fire immediately on the next boot.

### Timer management

```bash
# Status
systemctl status froxlor-backup.timer
systemctl status froxlor-backup.service

# Manual run (immediately, without waiting for the timer)
systemctl start froxlor-backup.service

# Follow live output
journalctl -u froxlor-backup.service -f

# Run history
journalctl -u froxlor-backup.service --since "7 days ago"

# When the timer will fire next
systemctl list-timers froxlor-backup.timer
```

### Changing the run time

```bash
# Edit the timer file
nano /etc/systemd/system/froxlor-backup.timer

# Then reload
systemctl daemon-reload
systemctl restart froxlor-backup.timer
```

---

## 15. Notifications

```yaml
notifications:
  enabled: true
  email_to: admin@example.com
  email_from: froxlor-backup@example.com
  smtp_host: localhost
  smtp_port: 25
  smtp_user: null
  smtp_password: null
  smtp_tls: false
  on_error: true     # send email on error
  on_success: false  # send email on successful backup
```

---

## 16. Troubleshooting

### Backup did not run

```bash
systemctl status froxlor-backup.service
journalctl -u froxlor-backup.service -n 50
```

### DB connection error

```bash
# Test connection manually
mysql -h localhost -u froxlor -p froxlor -e "SELECT COUNT(*) FROM panel_domains;"
```

Check the `froxlor_db` section in `config.yaml`.

### mysqldump: Access denied

```bash
# Test root access
mysql -h localhost -u root -p -e "SHOW DATABASES;"
```

Check the `db_root_servers` section in `config.yaml`. Root credentials are in `/var/www/froxlor/lib/userdata.inc.php` in the `$sql_root` array.

### rsync: Permission denied (publickey)

```bash
# Test SSH connection
ssh -i /root/.ssh/froxlor_backup_ed25519 -p 22 backupuser@backup.example.com "echo OK"

# Check that the public key is on the backup server
cat /root/.ssh/froxlor_backup_ed25519.pub
# → paste into /home/backupuser/.ssh/authorized_keys on the backup server
```

### Web backup is empty / docroot not found

Check `froxlor_paths.document_root_prefix` in `config.yaml`. It must match the value in Froxlor Admin → Settings → System → `system.documentroot_prefix`.

```bash
# Verify
ls /var/customers/webs/
```

### Mail backup is empty

Check `froxlor_paths.mail_home_dir` — it must match `system.vmail_homedir` in the Froxlor settings.

```bash
ls /var/customers/mail/
```

Also verify that the domain has `isemaildomain = 1` in the Froxlor DB:

```sql
SELECT domain, isemaildomain FROM panel_domains WHERE domain = 'example.com';
```

### Manual backup of a single domain with verbose output

```bash
froxlor-backup --domain example.com --verbose --skip-transfer
```

### Verifying backup integrity

```bash
# Check tar archive
tar -tzf /var/backups/froxlor-backup/example.com/2026-04-16/web/web.tar.gz | head -20

# Check SQL dump
zcat /var/backups/froxlor-backup/_db_web1/2026-04-16/databases/web1_wp.sql.gz | head -20
```
