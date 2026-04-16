# froxlor-backup

Automatický zálohovací systém pre **Froxlor** hosting panel.  
Zálohuje web súbory, mailboxy, MySQL/MariaDB databázy a logy **per-doménu / per-zákazníka**, prenáša zálohy na vzdialený server a umožňuje interaktívnu obnovu.

---

## Obsah

1. [Kontext – čo Froxlor ponúka natívne](#1-kontext--čo-froxlor-ponúka-natívne)
2. [Architektúra riešenia](#2-architektúra-riešenia)
3. [Súbory projektu](#3-súbory-projektu)
4. [Požiadavky](#4-požiadavky)
5. [Inštalácia](#5-inštalácia)
6. [Konfigurácia](#6-konfigurácia)
7. [Zálohovanie – froxlor-backup](#7-zálohovanie--froxlor-backup)
8. [Obnova – froxlor-restore](#8-obnova--froxlor-restore)
9. [Štruktúra zálohy na disku](#9-štruktúra-zálohy-na-disku)
10. [Retencia](#10-retencia)
11. [Vzdialené úložiská](#11-vzdialené-úložiská)
12. [Konzistencia pri živom serveri](#12-konzistencia-pri-živom-serveri)
13. [Permissions](#13-permissions)
14. [Automatizácia – systemd timer](#14-automatizácia--systemd-timer)
15. [Notifikácie](#15-notifikácie)
16. [Riešenie problémov](#16-riešenie-problémov)

---

## 1. Kontext – čo Froxlor ponúka natívne

Froxlor má vstavaný **DataDump** (do verzie 2.1.0 nazývaný „Backup") prístupný cez `customer_extras.php?page=export`. Ide o **manuálny export na požiadanie**, nie automatický zálohovací systém.

| Vlastnosť | DataDump (natívny) | froxlor-backup (tento projekt) |
|---|---|---|
| Spúšťanie | Ručne zákazníkom | Automaticky cez systemd timer |
| Granularita | Per-zákazník | Per-doména + per-zákazník (DB) |
| Cieľ zálohy | Docroot zákazníka | Externý server (SSH/S3/SFTP/…) |
| Retencia | Žiadna | Denná / týždenná / mesačná |
| Obnova cez panel | Nie | CLI wizard (`froxlor-restore`) |
| Logy | Nie | Voliteľne áno |
| Šifrovanie | GPG (voliteľne) | Natívne šifrovanie cieľa (napr. S3 SSE) |

Dokumentácia Froxlor k DataDump explicitne uvádza:
> *"Regular backups and/or snapshots are the responsibility of the admin."*

---

## 2. Architektúra riešenia

```
┌─────────────────────────────────────────────────────────────┐
│  Froxlor server                                             │
│                                                             │
│  systemd timer (02:30 každú noc)                           │
│       │                                                     │
│       ▼                                                     │
│  froxlor-backup.py                                         │
│       │                                                     │
│       ├── číta Froxlor MySQL DB (panel_domains,            │
│       │   panel_customers, mail_users, panel_databases)    │
│       │                                                     │
│       ├── per každú doménu:                                │
│       │     web.tar.gz      ← documentroot                 │
│       │     mail/*.tar.gz   ← maildir per schránku         │
│       │     logs.tar.gz     ← access/error logy (opt.)    │
│       │                                                     │
│       ├── per každého zákazníka (1×):                      │
│       │     databases/*.sql.gz  ← mysqldump každej DB      │
│       │                                                     │
│       ├── manifest.json  (metadata zálohy)                 │
│       │                                                     │
│       ├── retencia (maže staré lokálne zálohy)             │
│       │                                                     │
│       └── prenos na remote(s)                              │
│             rsync+SSH  alebo  rclone (S3/SFTP/B2/…)        │
│                                                             │
│  froxlor-restore.py                                        │
│       └── interaktívny wizard (web / mail / db / all)      │
└─────────────────────────────────────────────────────────────┘
```

### Froxlor DB tabuľky používané zálohovacím skriptom

| Tabuľka | Účel |
|---|---|
| `panel_domains` | Zoznam domén, `documentroot`, `isemaildomain` |
| `panel_customers` | Zákazníci, `loginname`, `guid` (uid/gid), `documentroot` |
| `mail_users` | Mailboxy – `homedir`, `maildir`, `domainid` |
| `panel_databases` | Databázy zákazníka – `databasename`, `dbserver` |

---

## 3. Súbory projektu

```
froxlor-backup/
├── froxlor-backup.py       – zálohovací engine
├── froxlor-restore.py      – interaktívny restore wizard
├── config.yaml.example     – vzorová konfigurácia
├── froxlor-backup.service  – systemd service jednotka
├── froxlor-backup.timer    – systemd timer (02:30 denne)
├── install.sh              – inštalačný skript
└── README.md               – táto dokumentácia
```

Po inštalácii:

```
/opt/froxlor-backup/            – skripty (root:root 700)
/etc/froxlor-backup/config.yaml – konfigurácia (root:root 600)
/var/backups/froxlor-backup/    – lokálne zálohy (root:root 700)
/var/log/froxlor-backup.log     – log (root:root 600)
/root/.ssh/froxlor_backup_ed25519  – SSH kľúč pre remote (root:root 600)
/usr/local/bin/froxlor-backup   – symlink
/usr/local/bin/froxlor-restore  – symlink
```

---

## 4. Požiadavky

### Systémové balíky

```bash
# Debian / Ubuntu
apt install python3 python3-pip tar gzip rsync openssh-client

# Pre rclone (S3 / Backblaze / SFTP / ...)
curl https://rclone.org/install.sh | bash

# MySQL / MariaDB klient (pre mysqldump)
apt install mariadb-client   # alebo mysql-client
```

### Python balíky

```bash
pip3 install PyMySQL PyYAML
```

---

## 5. Inštalácia

```bash
# 1. Stiahni / rozbaľ projekt
git clone <repo-url> /tmp/froxlor-backup
cd /tmp/froxlor-backup

# 2. Spusti inštalátor (vyžaduje root)
sudo bash install.sh
```

Inštalátor:
- Nainštaluje Python závislosti
- Skopíruje skripty do `/opt/froxlor-backup/` s oprávneniami `root:root 700`
- Vytvorí `/etc/froxlor-backup/config.yaml` z šablóny (ak neexistuje) s `root:root 600`
- Vytvorí zálohovací adresár `/var/backups/froxlor-backup/` s `root:root 700`
- Vygeneruje SSH kľúč `/root/.ssh/froxlor_backup_ed25519` (ak neexistuje)
- Nainštaluje a povolí systemd timer
- Vypíše tabuľku nastavených permissions pre kontrolu

```
# 3. Uprav konfiguráciu
nano /etc/froxlor-backup/config.yaml

# 4. Testovacia záloha (bez zápisu na disk)
froxlor-backup --dry-run --verbose

# 5. Prvá reálna záloha
froxlor-backup --verbose
```

---

## 6. Konfigurácia

Konfiguračný súbor: `/etc/froxlor-backup/config.yaml`

### froxlor_db

Froxlor databázový používateľ (read-only prístup stačí). Prihlasovacie údaje nájdeš v `/var/www/froxlor/lib/userdata.inc.php` v poli `$sql`.

```yaml
froxlor_db:
  host: localhost
  port: 3306
  socket: null          # alternatíva: /var/run/mysqld/mysqld.sock
  user: froxlor
  password: "heslo"
  name: froxlor
```

### db_root_servers

Root prístup k MySQL serverom pre `mysqldump`. Froxlor podporuje viac DB serverov – index musí zodpovedať `dbserver` ID v tabuľke `panel_databases`. Prihlasovacie údaje nájdeš v `userdata.inc.php` v poli `$sql_root`.

```yaml
db_root_servers:
  0:                    # dbserver=0 (default, jediný server)
    host: localhost
    port: 3306
    socket: null
    user: root
    password: "root_heslo"
  1:                    # druhý DB server (ak existuje)
    host: db2.internal
    port: 3306
    user: root
    password: "heslo2"
```

### froxlor_paths

Musí zodpovedať nastaveniu v Froxlor Admin → Settings → System → Paths.

```yaml
froxlor_paths:
  document_root_prefix: /var/customers/webs   # system.documentroot_prefix
  mail_home_dir: /var/customers/mail           # system.vmail_homedir
  logs_dir: /var/customers/logs                # system.logfiles_directory
```

### backup

```yaml
backup:
  web: true        # súbory z documentroot každej domény
  mail: true       # mailboxy (per doména cez mail_users.domainid)
  databases: true  # mysqldump všetkých DB zákazníka
  logs: false      # access/error logy (zvyčajne nie je potrebné)
```

### Filtrovanie domén / zákazníkov

```yaml
exclude_domains: ["test.example.com", "dev.example.com"]
exclude_customers: ["web_test"]
include_only_customers: []    # ak neprázdne, zálohuje IBA týchto zákazníkov
```

### remotes

Môžeš definovať viacero cieľov – všetky s `enabled: true` sa použijú.

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
    rclone_remote: wasabi                   # meno z `rclone config`
    path: moj-bucket/froxlor-backups
    rclone_config: /root/.config/rclone/rclone.conf
    rclone_extra_args: ["--transfers=4"]
```

### retention

```yaml
retention:
  daily: 7      # posledných 7 denných záloh
  weekly: 4     # 1 záloha/týždeň, posledné 4 týždne
  monthly: 6    # 1 záloha/mesiac, posledných 6 mesiacov
```

Retencia sa aplikuje na **lokálny adresár**. Keďže rsync používa `--delete`, zmazané lokálne zálohy sa automaticky zmažú aj na remote serveri.

---

## 7. Zálohovanie – froxlor-backup

### Použitie

```bash
# Zálohovanie všetkých domén
froxlor-backup

# Len jedna doména
froxlor-backup --domain example.com

# Len jeden zákazník (všetky jeho domény + DB)
froxlor-backup --customer web1

# Dry-run – iba vypíše čo by sa zálohovalo
froxlor-backup --dry-run --verbose

# Bez prenosu na remote (len lokálna záloha)
froxlor-backup --skip-transfer

# Bez aplikovania retencie
froxlor-backup --skip-retention
```

### Všetky prepínače

| Prepínač | Popis |
|---|---|
| `--config PATH` | Cesta ku konfiguráku (default: `/etc/froxlor-backup/config.yaml`) |
| `--domain NAME` | Zálohuj len túto doménu |
| `--customer NAME` | Zálohuj len tohto zákazníka (loginname) |
| `--skip-transfer` | Nevykonávaj prenos na remote |
| `--skip-retention` | Neaplikuj retenčnú politiku |
| `--dry-run` | Simulácia – nič nezapisuj |
| `--verbose / -v` | Podrobné logovanie (DEBUG level) |

### Čo záloha obsahuje

**Per doména:**

```
example.com/2026-04-16/
├── web/
│   └── web.tar.gz          ← celý documentroot domény
├── mail/
│   ├── info@example.com.tar.gz
│   └── admin@example.com.tar.gz
├── logs/
│   └── logs.tar.gz         ← len ak backup.logs: true
└── manifest.json
```

**Per zákazník (databázy):**

```
_db_web1/2026-04-16/
├── databases/
│   ├── web1_wp.sql.gz
│   └── web1_eshop.sql.gz
└── manifest.json
```

### manifest.json

Každá záloha obsahuje metadáta:

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

## 8. Obnova – froxlor-restore

### Interaktívny wizard

```bash
froxlor-restore
```

Prejde cez 4 kroky:

```
Krok 1 – výber entity:
  1)  [doména]    example.com        (posl. záloha: 2026-04-16, 12 celkom)
  2)  [doména]    shop.example.com   (posl. záloha: 2026-04-16,  7 celkom)
  3)  [databázy]  zákazník web1      (posl. záloha: 2026-04-16,  7 celkom)

Krok 2 – výber dátumu:
  1)  2026-04-16  (dnes)   [web, mail]
  2)  2026-04-15  (včera)  [web, mail]

Krok 3 – čo obnoviť:
  1)  web súbory (documentroot)
  2)  e-mailové schránky
  3)  databázy (SQL dump)
  4)  všetko dostupné

Krok 4 – potvrdenie:
  Entita : example.com
  Dátum  : 2026-04-15
  Typ    : web súbory (documentroot)
  Skutočne obnoviť? [y/N]:
```

### Neinteraktívne volanie

```bash
# Obnova web súborov domény z konkrétneho dátumu
froxlor-restore --domain example.com --date 2026-04-15 --type web

# Obnova DB zákazníka
froxlor-restore --domain web1 --type db

# Dry-run (bez zápisu)
froxlor-restore --domain example.com --type all --dry-run

# Výpis dostupných záloh
froxlor-restore --list
```

### Typy obnovy

| Typ | Čo obnoví | Bezpečnosť |
|---|---|---|
| `web` | Rozbalí `web.tar.gz` do `documentroot` | Pred obnovou vytvorí zálohu aktuálneho stavu ako `docroot.pre_restore_TIMESTAMP` |
| `mail` | Rozbalí maildir archívy do `homedir` | Výber konkrétnej schránky alebo všetkých naraz |
| `db` / `databases` | Importuje `.sql.gz` dumpy cez `mysql` klient | Potvrdenie pre každú DB zvlášť |
| `all` | Web + mail (+ DB ak ide o zákazníka) | Kombinácia vyššie |

### Výpis dostupných záloh

```bash
froxlor-restore --list
```

```
Dostupné zálohy v /var/backups/froxlor-backup:

Doména                                        Zálohy (novšie → staršie)
────────────────────────────────────────────────────────────────────────
  example.com                                 2026-04-16  [web, mail]  (7 záloh)
  shop.example.com                            2026-04-16  [web]        (7 záloh)

Zákazník (databázy)                           Posledná záloha
────────────────────────────────────────────────────────────────────────
  web1                                        2026-04-16  [databases]  (7 záloh)
```

---

## 9. Štruktúra zálohy na disku

### Lokálny adresár (`/var/backups/froxlor-backup/`)

```
/var/backups/froxlor-backup/
│
├── example.com/                    ← jedna doména
│   ├── 2026-04-16/                 ← dátum zálohy (ISO 8601)
│   │   ├── web/web.tar.gz
│   │   ├── mail/info@example.com.tar.gz
│   │   ├── mail/admin@example.com.tar.gz
│   │   └── manifest.json
│   ├── 2026-04-15/
│   └── 2026-04-09/                 ← týždenná záloha
│
├── shop.example.com/
│   └── 2026-04-16/
│       ├── web/web.tar.gz
│       └── manifest.json
│
└── _db_web1/                       ← databázy zákazníka web1
    ├── 2026-04-16/
    │   ├── databases/web1_wp.sql.gz
    │   ├── databases/web1_eshop.sql.gz
    │   └── manifest.json
    └── 2026-04-01/                 ← mesačná záloha
```

### Remote server (identická štruktúra)

Rsync synchronizuje celý lokálny adresár na remote vrátane mazania – štruktúra je identická. Pri rclone tiež.

---

## 10. Retencia

Algoritmus uchováva zálohy podľa troch úrovní súčasne:

```
Príklad s nastavením daily:7, weekly:4, monthly:6
(zálohy každý deň po dobu 2 mesiacov)

Apríl 2026:
  16 (dnes)   ← daily
  15          ← daily
  14          ← daily
  13          ← daily
  12          ← daily
  11          ← daily
  10          ← daily  + weekly (týždeň 15)
   9          ← ZMAZANÉ
   8          ← ZMAZANÉ
   7          ← ZMAZANÉ
   6          ← ZMAZANÉ
   5          ← ZMAZANÉ
   4          ← ZMAZANÉ
   3          ← weekly (týždeň 14)
  ...

Február 2026:
   1          ← monthly

November 2025: ZMAZANÉ (starší ako 6 mesiacov)
```

Retencia sa aplikuje na lokálny adresár. Rsync s `--delete` potom zmaže rovnaké zálohy aj na remote.

---

## 11. Vzdialené úložiská

### rsync + SSH

Najjednoduchšia voľba. Vyžaduje SSH prístup na backup server.

```bash
# Na backup serveri – vytvor používateľa a adresár
adduser --disabled-password backupuser
mkdir -p /mnt/backups/froxlor
chown backupuser:backupuser /mnt/backups/froxlor

# Skopíruj verejný kľúč (vygenerovaný install.sh)
cat /root/.ssh/froxlor_backup_ed25519.pub
# → vlož do /home/backupuser/.ssh/authorized_keys na backup serveri
```

V `config.yaml`:

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
# Nakonfiguruj rclone (interaktívne)
rclone config

# Otestuj
rclone ls wasabi:moj-bucket
```

V `config.yaml`:

```yaml
remotes:
  - name: s3-wasabi
    type: rclone
    enabled: true
    rclone_remote: wasabi
    path: moj-bucket/froxlor-backups
```

### Viacero remotes naraz

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

Oba sa použijú – záloha sa prenesie na oba ciele.

---

## 12. Konzistencia pri živom serveri

### MySQL / MariaDB – InnoDB

Skript používa `mysqldump --single-transaction --lock-tables=false`:

```
START TRANSACTION WITH CONSISTENT SNAPSHOT
  → REPEATABLE READ izolačná úroveň
  → žiadne table locky
  → aktívne zápisy pokračujú bez prerušenia
  → dump je konzistentný ku momentu spustenia
COMMIT
```

**InnoDB** (štandard vo všetkých moderných webových aplikáciách – WordPress, Joomla, PrestaShop, …): **plne konzistentné**.

### MySQL / MariaDB – MyISAM

MyISAM nepodporuje transakcie. `--lock-tables=false` vypne automatické zamykanie, ale dump **nemusí byť konzistentný** ak prebieha zápis. MyISAM je však zastaraný engine – väčšina webhostingových aplikácií ho nepoužíva od roku ~2015.

### Dovecot / Maildir

Dovecot používa **Maildir** formát. Každý email je samostatný súbor, dodaný **atomicky**:

```
1. Dovecot zapíše správu do  tmp/1234567890.hostname
2. atomic rename()        →  new/1234567890.hostname
```

Tar záloha môže vidieť súbor buď celý, alebo vôbec – čiastočne rozrobený email sa nestane. Ak rename prebehne počas tar-u, email jednoducho nebude v zálohe (tar vráti exit code 1, čo skript správne ignoruje – `returncode not in (0, 1)` → error, `== 1` → OK).

**Dovecot index súbory** (`.index`, `.index.log`, `.index.cache`) môžu byť zálohované v nekonzistentnom stave. To nie je kritické – Dovecot ich pri prvom prístupe po restore automaticky prebuduje.

Flag `--warning=no-file-changed` v tar príkaze potlačuje varovanie pre tieto súbory a nezahltí log zbytočnými hláškami.

### Súhrn konzistencie

| Typ dát | Technika | Konzistentné? | Poznámka |
|---|---|---|---|
| InnoDB tabuľky | `--single-transaction` | **Áno** | Žiadne locky |
| MyISAM tabuľky | `--lock-tables=false` | Čiastočne | Legacy engine, dnes výnimočný |
| Maildir súbory | tar + ignoruj exit 1 | **Áno** | Atomická dodávka |
| Dovecot indexy | tar + `--warning=no-file-changed` | Nie | Dovecot prebuduje automaticky |
| Web súbory | tar | **Áno** (statické) | Riziko len pri súboroch písaných aplikáciou |

---

## 13. Permissions

Inštalátor nastavuje nasledovné oprávnenia:

| Súbor / adresár | owner:group | perms | Dôvod |
|---|---|---|---|
| `/opt/froxlor-backup/` | root:root | `700` | Adresár nevidí nikto iný |
| `froxlor-backup.py` | root:root | `700` | Spustiteľný len rootom |
| `froxlor-restore.py` | root:root | `700` | Spustiteľný len rootom |
| `config.yaml.example` | root:root | `640` | Vzor bez citlivých údajov |
| `/etc/froxlor-backup/` | root:root | `700` | Adresár nevidí nikto iný |
| **`config.yaml`** | root:root | **`600`** | Obsahuje DB root heslo a SSH key path |
| `/var/backups/froxlor-backup/` | root:root | `700` | Zálohy obsahujú dáta zákazníkov |
| `/var/log/froxlor-backup.log` | root:root | `600` | Logy môžu obsahovať DB mená a cesty |
| `/root/.ssh/` | root:root | `700` | SSH vyžaduje – odmietne inak |
| `froxlor_backup_ed25519` | root:root | `600` | SSH vyžaduje – odmietne `640`/`644` |
| `froxlor_backup_ed25519.pub` | root:root | `644` | Verejný kľúč |
| `froxlor-backup.service` | root:root | `644` | systemd číta ako root |
| `froxlor-backup.timer` | root:root | `644` | systemd číta ako root |

> **Poznámka k symlinkom:** `/usr/local/bin/froxlor-backup` a `/usr/local/bin/froxlor-restore` sú symlinky – oprávnenia symlinku samotného sú irelevantné, bezpečnosť je daná targetom (`700`).

Pri **reinštalácii** (opätovnom spustení `install.sh`) sa permissions opravia na všetkých existujúcich súboroch – vrátane `config.yaml` ak by niekto omylom zmenil práva.

---

## 14. Automatizácia – systemd timer

### Ako funguje

```
froxlor-backup.timer  →  spúšťa  →  froxlor-backup.service
```

Timer beží každú noc o **02:30** s náhodným oneskorením 0–15 minút (`RandomizedDelaySec=900`) – to zabraňuje súbehu na serveroch kde beží viacero zálohovacích procesov.

`Persistent=true` znamená, že ak server bol o 02:30 vypnutý, timer sa spustí ihneď po najbližšom štarte.

### Správa timera

```bash
# Stav
systemctl status froxlor-backup.timer
systemctl status froxlor-backup.service

# Manuálne spustenie (okamžite, bez čakania na timer)
systemctl start froxlor-backup.service

# Sledovanie behu v reálnom čase
journalctl -u froxlor-backup.service -f

# História spustení
journalctl -u froxlor-backup.service --since "7 days ago"

# Kedy sa timer spustí nabudúce
systemctl list-timers froxlor-backup.timer
```

### Zmena času spustenia

```bash
# Zmeň čas v timer súbore
nano /etc/systemd/system/froxlor-backup.timer

# Potom reload
systemctl daemon-reload
systemctl restart froxlor-backup.timer
```

---

## 15. Notifikácie

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
  on_error: true     # pošli mail pri chybe
  on_success: false  # pošli mail po úspešnej zálohe
```

---

## 16. Riešenie problémov

### Záloha sa nespustila

```bash
systemctl status froxlor-backup.service
journalctl -u froxlor-backup.service -n 50
```

### Chyba pripojenia k DB

```bash
# Otestuj pripojenie ručne
mysql -h localhost -u froxlor -p froxlor -e "SELECT COUNT(*) FROM panel_domains;"
```

Skontroluj `froxlor_db` sekciu v `config.yaml`.

### mysqldump: Access denied

```bash
# Otestuj root prístup
mysql -h localhost -u root -p -e "SHOW DATABASES;"
```

Skontroluj `db_root_servers` sekciu v `config.yaml`. Root prihlasovacie údaje sú v `/var/www/froxlor/lib/userdata.inc.php` v poli `$sql_root`.

### rsync: Permission denied (publickey)

```bash
# Otestuj SSH pripojenie
ssh -i /root/.ssh/froxlor_backup_ed25519 -p 22 backupuser@backup.example.com "echo OK"

# Skontroluj či je verejný kľúč na backup serveri
cat /root/.ssh/froxlor_backup_ed25519.pub
# → vlož do /home/backupuser/.ssh/authorized_keys na backup serveri
```

### Web záloha je prázdna / docroot nenájdený

Skontroluj `froxlor_paths.document_root_prefix` v `config.yaml`. Musí zodpovedať hodnote v Froxlor Admin → Settings → System → `system.documentroot_prefix`.

```bash
# Overiť
ls /var/customers/webs/
```

### Záloha mail je prázdna

Skontroluj `froxlor_paths.mail_home_dir` – musí zodpovedať `system.vmail_homedir` vo Froxlor nastaveniach.

```bash
ls /var/customers/mail/
```

Tiež over či doména má `isemaildomain = 1` v Froxlor DB:

```sql
SELECT domain, isemaildomain FROM panel_domains WHERE domain = 'example.com';
```

### Manuálna záloha jednej domény s podrobným výpisom

```bash
froxlor-backup --domain example.com --verbose --skip-transfer
```

### Overenie integrity zálohy

```bash
# Skontroluj tar archív
tar -tzf /var/backups/froxlor-backup/example.com/2026-04-16/web/web.tar.gz | head -20

# Skontroluj SQL dump
zcat /var/backups/froxlor-backup/_db_web1/2026-04-16/databases/web1_wp.sql.gz | head -20
```
