#!/usr/bin/env python3
"""
froxlor-restore  –  Interactive restore of a Froxlor domain/customer backup

Usage:
  froxlor-restore.py [--config /etc/froxlor-backup/config.yaml]
                     [--domain example.com] [--date 2026-04-15]
                     [--type web|mail|db|all]
                     [--list] [--dry-run]
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import pymysql
    import yaml
except ImportError:
    print("Missing dependencies: pip install PyMySQL PyYAML", file=sys.stderr)
    sys.exit(1)

VERSION = "1.0.0"
BOLD = "\033[1m"
RED = "\033[31m"
GRN = "\033[32m"
YEL = "\033[33m"
CYN = "\033[36m"
RST = "\033[0m"


def ok(s): return f"{GRN}✓{RST} {s}"
def warn(s): return f"{YEL}⚠{RST}  {s}"
def err(s): return f"{RED}✗{RST} {s}"
def info(s): return f"{CYN}→{RST} {s}"


# ─────────────────────────────────────────────────────────────
# Configuration + DB (same as in the backup script)
# ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def db_connect(cfg: dict) -> pymysql.connections.Connection:
    db = cfg["froxlor_db"]
    kwargs = dict(
        host=db["host"],
        port=int(db.get("port") or 3306),
        user=db["user"],
        password=db["password"],
        database=db["name"],
        charset="utf8",
        cursorclass=pymysql.cursors.DictCursor,
    )
    if db.get("socket"):
        kwargs["unix_socket"] = db["socket"]
        kwargs.pop("host", None)
        kwargs.pop("port", None)
    return pymysql.connect(**kwargs)


def get_domain_info(conn, domain_name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id AS domain_id, d.domain, d.documentroot AS domain_docroot,
                   d.isemaildomain, c.customerid, c.loginname, c.guid,
                   c.documentroot AS customer_docroot
            FROM panel_domains d
            JOIN panel_customers c ON d.customerid = c.customerid
            WHERE d.domain = %s
        """, (domain_name,))
        return cur.fetchone()


def get_domain_mail_accounts(conn, domain_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT email, homedir, maildir
            FROM mail_users WHERE domainid = %s AND postfix = 'Y'
        """, (domain_id,))
        return cur.fetchall()


def get_db_root_credentials(cfg: dict, dbserver: int) -> dict:
    servers = cfg.get("db_root_servers", {})
    return servers.get(dbserver) or servers.get(str(dbserver))


def find_mysqldump():
    for p in ["/usr/bin/mariadb-dump", "/usr/bin/mysqldump", "/usr/local/bin/mysqldump"]:
        if os.path.isfile(p): return p
    return shutil.which("mysqldump") or shutil.which("mariadb-dump")


def find_mysql():
    for p in ["/usr/bin/mariadb", "/usr/bin/mysql", "/usr/local/bin/mysql"]:
        if os.path.isfile(p): return p
    return shutil.which("mysql") or shutil.which("mariadb")


# ─────────────────────────────────────────────────────────────
# Backup directory scanning
# ─────────────────────────────────────────────────────────────

def scan_backups(backup_base: Path) -> dict:
    """
    Returns a dict:
      {
        "example.com":  { "type": "domain",    "slug": "example.com", "dates": ["2026-04-16", ...] },
        "_db_web1":     { "type": "databases",  "slug": "_db_web1", "customer": "web1", "dates": [...] },
      }
    """
    result = {}
    if not backup_base.exists():
        return result

    for entity_dir in sorted(backup_base.iterdir()):
        if not entity_dir.is_dir():
            continue

        dates = sorted(
            [sub.name for sub in entity_dir.iterdir()
             if re.match(r"^\d{4}-\d{2}-\d{2}$", sub.name) and sub.is_dir()],
            reverse=True
        )
        if not dates:
            continue

        slug = entity_dir.name
        if slug.startswith("_db_"):
            result[slug] = {
                "type": "databases",
                "slug": slug,
                "customer": slug[4:],
                "dates": dates,
                "path": entity_dir,
            }
        else:
            result[slug] = {
                "type": "domain",
                "slug": slug,
                "domain": slug,
                "dates": dates,
                "path": entity_dir,
            }
    return result


def read_manifest(backup_dir: Path) -> dict:
    mf = backup_dir / "manifest.json"
    if mf.exists():
        with open(mf) as f:
            return json.load(f)
    return {}


def list_backup_contents(backup_dir: Path) -> list:
    """Detect what a backup contains by inspecting its directory structure."""
    contents = []
    if (backup_dir / "web").exists() and any((backup_dir / "web").iterdir()):
        contents.append("web")
    if (backup_dir / "mail").exists() and any((backup_dir / "mail").iterdir()):
        contents.append("mail")
    if (backup_dir / "databases").exists() and any((backup_dir / "databases").iterdir()):
        contents.append("databases")
    if (backup_dir / "logs").exists():
        contents.append("logs")
    return contents


# ─────────────────────────────────────────────────────────────
# Listing available backups
# ─────────────────────────────────────────────────────────────

def cmd_list(backup_base: Path):
    backups = scan_backups(backup_base)
    if not backups:
        print(warn("No backups found in " + str(backup_base)))
        return

    print(f"\n{BOLD}Available backups in {backup_base}:{RST}\n")

    domains_bk = {k: v for k, v in backups.items() if v["type"] == "domain"}
    db_bk = {k: v for k, v in backups.items() if v["type"] == "databases"}

    if domains_bk:
        print(f"{BOLD}{'Domain':<45} {'Backups (newest → oldest)'}{RST}")
        print("─" * 80)
        for slug, bk in sorted(domains_bk.items()):
            latest = bk["dates"][0] if bk["dates"] else "—"
            contents = list_backup_contents(bk["path"] / latest)
            print(f"  {bk['domain']:<43} {latest}  [{', '.join(contents)}]  ({len(bk['dates'])} backups)")

    if db_bk:
        print(f"\n{BOLD}{'Customer (databases)':<45} {'Latest backup'}{RST}")
        print("─" * 80)
        for slug, bk in sorted(db_bk.items()):
            latest = bk["dates"][0] if bk["dates"] else "—"
            contents = list_backup_contents(bk["path"] / latest)
            print(f"  {bk['customer']:<43} {latest}  [{', '.join(contents)}]  ({len(bk['dates'])} backups)")
    print()


# ─────────────────────────────────────────────────────────────
# Interactive selection helpers
# ─────────────────────────────────────────────────────────────

def choose(prompt: str, options: list, allow_back=True) -> int:
    """Display a numbered list and return the chosen index (0-based). -1 = back/cancel."""
    print(f"\n{BOLD}{prompt}{RST}")
    for i, opt in enumerate(options, 1):
        print(f"  {CYN}{i:>3}{RST})  {opt}")
    if allow_back:
        print(f"  {YEL}  0{RST})  [Back / Cancel]")
    while True:
        try:
            raw = input("\nYour choice: ").strip()
            n = int(raw)
            if allow_back and n == 0:
                return -1
            if 1 <= n <= len(options):
                return n - 1
        except (ValueError, EOFError):
            pass
        print(warn("Invalid choice, please try again."))


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"\n{BOLD}{prompt} [y/N]{RST}: ").strip().lower()
        return ans in ("y", "yes")
    except EOFError:
        return False


# ─────────────────────────────────────────────────────────────
# Restore operations
# ─────────────────────────────────────────────────────────────

def restore_web(backup_dir: Path, domain_info: dict, cfg: dict, dry_run: bool) -> bool:
    web_archive = backup_dir / "web" / "web.tar.gz"
    if not web_archive.exists():
        print(err(f"web.tar.gz not found in {backup_dir / 'web'}"))
        return False

    docroot = domain_info["domain_docroot"] or ""
    if not docroot or docroot == "/":
        docroot = os.path.join(
            cfg["froxlor_paths"]["document_root_prefix"],
            domain_info["loginname"],
            domain_info["domain"],
        )
        if not os.path.isdir(docroot):
            docroot = os.path.join(
                cfg["froxlor_paths"]["document_root_prefix"],
                domain_info["loginname"],
            )

    print(info(f"Target directory: {docroot}"))

    if not os.path.isdir(docroot):
        print(warn(f"Directory does not exist, will be created: {docroot}"))
        if not dry_run:
            os.makedirs(docroot, exist_ok=True)

    # Snapshot current state before overwriting
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    pre_backup = f"{docroot}.pre_restore_{now_str}"
    print(info(f"Snapshot of current state before restore: {pre_backup}"))

    if not dry_run:
        if os.path.isdir(docroot):
            shutil.copytree(docroot, pre_backup, symlinks=True, ignore_dangling_symlinks=True)
        result = subprocess.run(
            ["tar", "--extract", "--gzip",
             "--file", str(web_archive),
             "--directory", docroot,
             "--overwrite"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(err(f"tar extract failed (rc={result.returncode}): {result.stderr[:400]}"))
            return False
        # Fix ownership
        guid = domain_info.get("customer_guid") or domain_info.get("guid")
        if guid:
            subprocess.run(["chown", "-R", f"{guid}:{guid}", docroot], check=False)
    else:
        print(info(f"[DRY-RUN] tar extract {web_archive} → {docroot}"))

    print(ok(f"Web restored to: {docroot}"))
    return True


def restore_mail(backup_dir: Path, domain_info: dict, conn, dry_run: bool) -> bool:
    mail_dir = backup_dir / "mail"
    if not mail_dir.exists():
        print(err(f"Mail directory not found: {mail_dir}"))
        return False

    mail_accounts = get_domain_mail_accounts(conn, domain_info["domain_id"])
    if not mail_accounts:
        print(warn("No mailboxes for this domain."))
        return True

    archives = sorted(mail_dir.glob("*.tar.gz"))
    if not archives:
        print(err("No mail archives found."))
        return False

    # Match archives to mailbox accounts
    account_map = {a["email"]: a for a in mail_accounts}
    archive_map = {}
    for arch in archives:
        # Reverse safe_name: match filename back to email address
        base = arch.stem.replace(".tar", "")  # e.g. "info_example_com"
        for email in account_map:
            safe = re.sub(r"[^a-zA-Z0-9._-]", "_", email)
            if safe == base:
                archive_map[email] = arch
                break

    print(f"\n{BOLD}Available mailboxes:{RST}")
    all_emails = list(account_map.keys())
    for i, email in enumerate(all_emails, 1):
        arch = archive_map.get(email)
        status = f"{GRN}archive found{RST}" if arch else f"{YEL}archive not found{RST}"
        print(f"  {i:>3}) {email}  — {status}")
    print(f"  {CYN}  0{RST}) Restore ALL available")

    while True:
        try:
            raw = input("\nSelect mailbox (0=all): ").strip()
            n = int(raw)
            if 0 <= n <= len(all_emails):
                break
        except (ValueError, EOFError):
            pass
        print(warn("Invalid choice"))

    selected = all_emails if n == 0 else [all_emails[n - 1]]

    ok_count = 0
    for email in selected:
        arch = archive_map.get(email)
        if not arch:
            print(warn(f"Archive for {email} not found, skipping."))
            continue

        acct = account_map[email]
        homedir = (acct["homedir"] or "").rstrip("/")
        maildir = (acct["maildir"] or "").strip("/")

        if not homedir:
            print(err(f"Unknown homedir for {email}"))
            continue

        print(info(f"Restoring: {email} → {homedir}/{maildir}"))

        if not dry_run:
            os.makedirs(homedir, exist_ok=True)
            result = subprocess.run(
                ["tar", "--extract", "--gzip",
                 "--file", str(arch),
                 "--directory", homedir,
                 "--overwrite"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(err(f"Restore of {email} failed (rc={result.returncode}): {result.stderr[:300]}"))
                continue
            uid = acct.get("uid") or 0
            gid = acct.get("gid") or 0
            if uid:
                subprocess.run(["chown", "-R", f"{uid}:{gid}", homedir], check=False)
        else:
            print(info(f"[DRY-RUN] tar extract {arch.name} → {homedir}"))

        print(ok(f"Mailbox restored: {email}"))
        ok_count += 1

    return ok_count > 0


def restore_databases(backup_dir: Path, customer_slug: str, cfg: dict, conn, dry_run: bool) -> bool:
    db_dir = backup_dir / "databases"
    if not db_dir.exists():
        print(err(f"Databases directory not found: {db_dir}"))
        return False

    dumps = sorted(db_dir.glob("*.sql.gz"))
    if not dumps:
        print(err("No SQL dumps found."))
        return False

    mysql_cli = find_mysql()
    if not mysql_cli:
        print(err("mysql/mariadb client not found!"))
        return False

    # Fetch the customer's databases from Froxlor for validation
    customer_name = customer_slug[4:] if customer_slug.startswith("_db_") else customer_slug
    customer_dbs = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pd.databasename, pd.dbserver
            FROM panel_databases pd
            JOIN panel_customers pc ON pd.customerid = pc.customerid
            WHERE pc.loginname = %s
        """, (customer_name,))
        for row in cur.fetchall():
            customer_dbs[row["databasename"]] = row["dbserver"]

    # Select databases to restore
    print(f"\n{BOLD}Available SQL dumps:{RST}")
    for i, dump in enumerate(dumps, 1):
        db_name = dump.name.replace(".sql.gz", "")
        frx_status = f"{GRN}(exists in Froxlor){RST}" if db_name in customer_dbs else f"{YEL}(not found in Froxlor){RST}"
        size_mb = dump.stat().st_size / 1024 / 1024
        print(f"  {i:>3}) {db_name:<40} {size_mb:.1f} MB  {frx_status}")
    print(f"  {CYN}  0{RST}) Restore ALL")

    while True:
        try:
            raw = input("\nSelect database (0=all): ").strip()
            n = int(raw)
            if 0 <= n <= len(dumps):
                break
        except (ValueError, EOFError):
            pass
        print(warn("Invalid choice"))

    selected = dumps if n == 0 else [dumps[n - 1]]

    ok_count = 0
    for dump in selected:
        db_name = dump.name.replace(".sql.gz", "")
        dbserver = customer_dbs.get(db_name, 0)
        srv = get_db_root_credentials(cfg, dbserver)

        if not srv:
            print(err(f"No credentials found for dbserver {dbserver} (db: {db_name})"))
            continue

        print(info(f"Restoring DB: {db_name} (dbserver={dbserver})"))

        if not dry_run:
            print(warn(f"WARNING: Existing data in '{db_name}' will be OVERWRITTEN!"))
            if not confirm(f"Continue restoring '{db_name}'?"):
                print("Skipped.")
                continue

            # Write a temporary .my.cnf file with credentials
            section = "mariadb" if "mariadb" in mysql_cli else "mysql"
            fd, mycnf_file = tempfile.mkstemp(prefix="frxrst_", suffix=".cnf")
            cnf = f"[{section}]\npassword={srv['password']}\nhost={srv['host']}\n"
            if srv.get("port"):
                cnf += f"port={srv['port']}\n"
            elif srv.get("socket"):
                cnf += f"socket={srv['socket']}\n"
            os.write(fd, cnf.encode())
            os.close(fd)
            os.chmod(mycnf_file, 0o600)

            try:
                mysql_cmd = [mysql_cli,
                             f"--defaults-file={mycnf_file}",
                             f"-u{srv['user']}",
                             db_name]
                with open(dump, "rb") as f_dump:
                    p_gz = subprocess.Popen(["gunzip", "-c"], stdin=f_dump, stdout=subprocess.PIPE)
                    p_mysql = subprocess.Popen(mysql_cmd, stdin=p_gz.stdout,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    p_gz.stdout.close()
                    _, mysql_err = p_mysql.communicate()
                    p_gz.wait()

                if p_mysql.returncode != 0:
                    print(err(f"Import of {db_name} failed: {mysql_err.decode(errors='replace')[:400]}"))
                    continue
            finally:
                os.unlink(mycnf_file)
        else:
            print(info(f"[DRY-RUN] gunzip {dump.name} | mysql {db_name}"))

        print(ok(f"DB restored: {db_name}"))
        ok_count += 1

    return ok_count > 0


# ─────────────────────────────────────────────────────────────
# Interactive wizard
# ─────────────────────────────────────────────────────────────

def interactive_restore(backup_base: Path, cfg: dict,
                        pre_domain: str, pre_date: str, pre_type: str,
                        dry_run: bool):

    conn = db_connect(cfg)
    backups = scan_backups(backup_base)

    if not backups:
        print(err("No backups found in " + str(backup_base)))
        sys.exit(1)

    # ── Step 1: Select entity (domain / customer databases) ──
    all_entities = sorted(backups.keys())

    # Split into domains vs databases
    domain_entities = [k for k in all_entities if backups[k]["type"] == "domain"]
    db_entities = [k for k in all_entities if backups[k]["type"] == "databases"]

    if pre_domain:
        # Direct selection via CLI argument – try domain first, then db slug
        if pre_domain in backups:
            chosen_entity = pre_domain
        elif f"_db_{pre_domain}" in backups:
            chosen_entity = f"_db_{pre_domain}"
        else:
            print(err(f"No backup found for '{pre_domain}'."))
            sys.exit(1)
    else:
        print(f"\n{BOLD}╔═══════════════════════════════════════╗")
        print(f"║   froxlor-restore {VERSION:<20}║")
        print(f"╚═══════════════════════════════════════╝{RST}")

        options = []
        for k in domain_entities:
            bk = backups[k]
            latest = bk["dates"][0] if bk["dates"] else "?"
            options.append(f"{CYN}[domain]{RST}     {bk['domain']:<40} (latest: {latest}, {len(bk['dates'])} total)")
        for k in db_entities:
            bk = backups[k]
            latest = bk["dates"][0] if bk["dates"] else "?"
            options.append(f"{YEL}[databases]{RST}  customer {bk['customer']:<33} (latest: {latest}, {len(bk['dates'])} total)")

        all_entity_keys = domain_entities + db_entities
        idx = choose("Select what you want to restore:", options, allow_back=False)
        chosen_entity = all_entity_keys[idx]

    bk = backups[chosen_entity]

    # ── Step 2: Select date ──
    if pre_date:
        if pre_date not in bk["dates"]:
            print(err(f"No backup for date '{pre_date}' found for '{chosen_entity}'."))
            sys.exit(1)
        chosen_date = pre_date
    else:
        date_options = []
        for d in bk["dates"]:
            backup_dir = bk["path"] / d
            contents = list_backup_contents(backup_dir)
            today = datetime.date.today().isoformat()
            yest = (datetime.date.today() - datetime.timedelta(1)).isoformat()
            label = f"{d}"
            if d == today: label += "  (today)"
            elif d == yest: label += "  (yesterday)"
            label += f"  [{', '.join(contents)}]"
            date_options.append(label)

        idx = choose(f"Select backup date for '{chosen_entity}':", date_options, allow_back=True)
        if idx == -1:
            print("Cancelled.")
            sys.exit(0)
        chosen_date = bk["dates"][idx]

    backup_dir = bk["path"] / chosen_date
    contents = list_backup_contents(backup_dir)
    print(info(f"Backup: {backup_dir}  [contains: {', '.join(contents)}]"))

    # ── Step 3: Select restore type ──
    restore_type_map = {
        "web": "web files (documentroot)",
        "mail": "email mailboxes",
        "databases": "databases (SQL dump)",
        "all": "everything available",
    }

    if pre_type:
        chosen_type = pre_type.lower()
    else:
        available_types = [t for t in ["web", "mail", "databases"] if t in contents]
        if "web" in available_types and "mail" in available_types:
            available_types.append("all")

        if not available_types:
            print(err("Backup contains no restorable data."))
            sys.exit(1)

        type_options = [restore_type_map.get(t, t) for t in available_types]
        idx = choose("What do you want to restore?", type_options, allow_back=True)
        if idx == -1:
            print("Cancelled.")
            sys.exit(0)
        chosen_type = available_types[idx]

    # ── Step 4: Confirmation ──
    print(f"\n{BOLD}{'─'*55}")
    print(f"  Entity : {chosen_entity}")
    print(f"  Date   : {chosen_date}")
    print(f"  Type   : {restore_type_map.get(chosen_type, chosen_type)}")
    if dry_run:
        print(f"  MODE   : {YEL}DRY-RUN{RST} (no changes will be made)")
    print(f"{'─'*55}{RST}")

    if not dry_run and not confirm("Proceed with restore?"):
        print("Cancelled.")
        sys.exit(0)

    # ── Step 5: Restore ──
    success = True

    if bk["type"] == "domain":
        domain_info = get_domain_info(conn, bk["domain"])
        if not domain_info:
            print(err(f"Domain '{bk['domain']}' not found in Froxlor DB!"))
            sys.exit(1)

        if chosen_type in ("web", "all") and "web" in contents:
            print(f"\n{BOLD}── Restoring web files ──{RST}")
            if not restore_web(backup_dir, domain_info, cfg, dry_run):
                success = False

        if chosen_type in ("mail", "all") and "mail" in contents:
            print(f"\n{BOLD}── Restoring mailboxes ──{RST}")
            if not restore_mail(backup_dir, domain_info, conn, dry_run):
                success = False

    elif bk["type"] == "databases":
        if chosen_type in ("databases", "db", "all"):
            print(f"\n{BOLD}── Restoring databases ──{RST}")
            if not restore_databases(backup_dir, bk["slug"], cfg, conn, dry_run):
                success = False

    conn.close()

    print()
    if success:
        print(ok("Restore completed successfully."))
    else:
        print(err("Restore finished with errors. Check the output above."))
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="froxlor-restore – interactive backup restore"
    )
    p.add_argument("--config", default="/etc/froxlor-backup/config.yaml")
    p.add_argument("--domain",    help="Pre-select domain or customer (skips menu)")
    p.add_argument("--date",      help="Pre-select backup date (YYYY-MM-DD)")
    p.add_argument("--type",      choices=["web", "mail", "db", "databases", "all"],
                   help="Pre-select restore type")
    p.add_argument("--list",      action="store_true",
                   help="List available backups and exit")
    p.add_argument("--dry-run",   action="store_true",
                   help="Simulate restore – no files will be overwritten")
    args = p.parse_args()

    if not os.path.exists(args.config):
        print(err(f"Configuration file not found: {args.config}"))
        print(f"  Copy from: cp /opt/froxlor-backup/config.yaml.example {args.config}")
        sys.exit(1)

    cfg = load_config(args.config)
    backup_base = Path(cfg.get("local_backup_dir", "/var/backups/froxlor-backup"))

    if args.list:
        cmd_list(backup_base)
        return

    # Normalize type alias
    restore_type = args.type
    if restore_type == "db":
        restore_type = "databases"

    interactive_restore(
        backup_base=backup_base,
        cfg=cfg,
        pre_domain=args.domain,
        pre_date=args.date,
        pre_type=restore_type,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
