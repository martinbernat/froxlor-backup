#!/usr/bin/env python3
"""
froxlor-backup  -  Automatic per-domain backup for the Froxlor hosting panel
Backs up: web files, mailboxes, MySQL dumps, logs
Transfer: rsync+SSH or rclone (S3, SFTP, Backblaze B2, ...)
Retention: daily / weekly / monthly

Usage:
  froxlor-backup.py [--config /etc/froxlor-backup/config.yaml]
                    [--domain example.com | --customer web1]
                    [--skip-transfer] [--dry-run] [--verbose]

Requirements (pip):
  pip install PyMySQL PyYAML
"""

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

try:
    import pymysql
    import yaml
except ImportError:
    print("Missing dependencies: pip install PyMySQL PyYAML", file=sys.stderr)
    sys.exit(1)

VERSION = "1.0.0"
LOG = logging.getLogger("froxlor-backup")


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg


def setup_logging(cfg: dict, verbose: bool):
    level = logging.DEBUG if verbose else getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler()]
    log_file = cfg.get("log_file")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# ─────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────

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


def get_domains(conn, cfg: dict) -> list:
    """Return all active top-level domains with their customer info."""
    exclude_domains = set(cfg.get("exclude_domains") or [])
    exclude_customers = set(cfg.get("exclude_customers") or [])
    only_customers = set(cfg.get("include_only_customers") or [])

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                d.id            AS domain_id,
                d.domain,
                d.documentroot  AS domain_docroot,
                d.isemaildomain,
                d.speciallogfile,
                d.writeaccesslog,
                d.writeerrorlog,
                c.customerid,
                c.loginname,
                c.guid          AS customer_guid,
                c.documentroot  AS customer_docroot
            FROM panel_domains  d
            JOIN panel_customers c ON d.customerid = c.customerid
            WHERE d.deactivated  = 0
              AND d.parentdomainid = 0
              AND c.deactivated  = 0
            ORDER BY c.loginname, d.domain
        """)
        rows = cur.fetchall()

    result = []
    for row in rows:
        if row["domain"] in exclude_domains:
            continue
        if row["loginname"] in exclude_customers:
            continue
        if only_customers and row["loginname"] not in only_customers:
            continue
        result.append(row)
    return result


def get_domain_mail_accounts(conn, domain_id: int) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT email, homedir, maildir
            FROM mail_users
            WHERE domainid = %s AND postfix = 'Y'
        """, (domain_id,))
        return cur.fetchall()


def get_customer_databases(conn, customerid: int) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT databasename, dbserver
            FROM panel_databases
            WHERE customerid = %s
            ORDER BY dbserver, databasename
        """, (customerid,))
        return cur.fetchall()


# ─────────────────────────────────────────────────────────────
# Backup - web
# ─────────────────────────────────────────────────────────────

def backup_web(domain: dict, backup_dir: Path, cfg: dict) -> bool:
    docroot = domain["domain_docroot"] or ""
    if not docroot or docroot == "/":
        # Froxlor default path: document_root_prefix / loginname / domain
        docroot = os.path.join(
            cfg["froxlor_paths"]["document_root_prefix"],
            domain["loginname"],
            domain["domain"],
        )
        # fallback: customer root
        if not os.path.isdir(docroot):
            docroot = os.path.join(
                cfg["froxlor_paths"]["document_root_prefix"],
                domain["loginname"],
            )

    if not os.path.isdir(docroot):
        LOG.warning("[%s] web docroot does not exist: %s", domain["domain"], docroot)
        return False

    web_dir = backup_dir / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    archive = web_dir / "web.tar.gz"

    LOG.info("[%s] backing up web: %s → %s", domain["domain"], docroot, archive)
    result = subprocess.run(
        ["tar", "--create", "--gzip",
         "--file", str(archive),
         "--directory", docroot,
         "--one-file-system",
         "."],
        capture_output=True, text=True
    )
    # returncode 1 = some files changed during archiving (OK)
    if result.returncode not in (0, 1):
        LOG.error("[%s] tar web failed (rc=%d): %s", domain["domain"], result.returncode, result.stderr[:500])
        return False

    size_mb = archive.stat().st_size / 1024 / 1024
    LOG.info("[%s] web backup done: %.1f MB", domain["domain"], size_mb)
    return True


# ─────────────────────────────────────────────────────────────
# Backup - mail
# ─────────────────────────────────────────────────────────────

def backup_mail(domain: dict, mail_accounts: list, backup_dir: Path) -> bool:
    if not mail_accounts:
        LOG.debug("[%s] no mailboxes", domain["domain"])
        return True

    mail_dir = backup_dir / "mail"
    mail_dir.mkdir(parents=True, exist_ok=True)
    success = True

    for account in mail_accounts:
        homedir = (account.get("homedir") or "").rstrip("/")
        maildir = (account.get("maildir") or "").strip("/")

        if not homedir or not os.path.isdir(homedir):
            LOG.warning("[%s] mail homedir does not exist: %s (%s)", domain["domain"], homedir, account["email"])
            continue

        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", account["email"])
        archive = mail_dir / f"{safe_name}.tar.gz"

        LOG.info("[%s] backing up mail: %s → %s", domain["domain"], account["email"], archive.name)
        result = subprocess.run(
            ["tar", "--create", "--gzip",
             "--file", str(archive),
             "--directory", homedir,
             "--one-file-system",
             "--warning=no-file-changed",  # exit 1 on changed file is normal with a live Dovecot
             f"./{maildir}"],
            capture_output=True, text=True
        )
        if result.returncode not in (0, 1):
            LOG.error("[%s] tar mail failed for %s (rc=%d): %s",
                      domain["domain"], account["email"], result.returncode, result.stderr[:300])
            success = False
        else:
            size_mb = archive.stat().st_size / 1024 / 1024
            LOG.info("[%s] mail backup: %s  %.1f MB", domain["domain"], account["email"], size_mb)

    return success


# ─────────────────────────────────────────────────────────────
# Backup - logs
# ─────────────────────────────────────────────────────────────

def backup_logs(domain: dict, backup_dir: Path, cfg: dict) -> bool:
    import glob
    logs_base = cfg["froxlor_paths"].get("logs_dir", "/var/customers/logs")
    cust_logs = os.path.join(logs_base, domain["loginname"])

    if not os.path.isdir(cust_logs):
        LOG.debug("[%s] log directory does not exist: %s", domain["domain"], cust_logs)
        return True

    # look for files starting with the domain name
    log_files = (
        glob.glob(os.path.join(cust_logs, f"{domain['domain']}-access*")) +
        glob.glob(os.path.join(cust_logs, f"{domain['domain']}-error*"))
    )
    if not log_files:
        LOG.debug("[%s] no log files found", domain["domain"])
        return True

    logs_dir = backup_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    archive = logs_dir / "logs.tar.gz"

    LOG.info("[%s] backing up logs: %d files", domain["domain"], len(log_files))
    result = subprocess.run(
        ["tar", "--create", "--gzip",
         "--file", str(archive),
         "--directory", cust_logs] +
        [os.path.basename(f) for f in log_files],
        capture_output=True, text=True
    )
    if result.returncode not in (0, 1):
        LOG.error("[%s] tar logs failed (rc=%d): %s", domain["domain"], result.returncode, result.stderr[:300])
        return False

    LOG.info("[%s] logs backup done: %.1f MB", domain["domain"], archive.stat().st_size / 1024 / 1024)
    return True


# ─────────────────────────────────────────────────────────────
# Backup - databases
# ─────────────────────────────────────────────────────────────

def find_mysqldump() -> Optional[str]:
    for p in ["/usr/bin/mariadb-dump", "/usr/bin/mysqldump", "/usr/local/bin/mysqldump"]:
        if os.path.isfile(p):
            return p
    return shutil.which("mysqldump") or shutil.which("mariadb-dump")


def get_db_root_credentials(cfg: dict, dbserver: int) -> Optional[dict]:
    servers = cfg.get("db_root_servers", {})
    srv = servers.get(dbserver) or servers.get(str(dbserver))
    if not srv:
        LOG.error("No credentials found for dbserver=%d in config.yaml (db_root_servers)", dbserver)
        return None
    return srv


def backup_databases(conn, customer: dict, backup_dir: Path, cfg: dict) -> bool:
    databases = get_customer_databases(conn, customer["customerid"])
    if not databases:
        LOG.debug("[%s] no databases", customer["loginname"])
        return True

    mysqldump = find_mysqldump()
    if not mysqldump:
        LOG.error("mysqldump/mariadb-dump not found! Install mysql-client / mariadb-client.")
        return False

    db_dir = backup_dir / "databases"
    db_dir.mkdir(parents=True, exist_ok=True)

    section = "mariadb-dump" if "mariadb" in mysqldump else "mysqldump"
    success = True
    current_dbserver = -1
    mycnf_file = None

    try:
        for db in databases:
            dbserver = db["dbserver"]

            if dbserver != current_dbserver:
                if mycnf_file and os.path.exists(mycnf_file):
                    os.unlink(mycnf_file)
                srv = get_db_root_credentials(cfg, dbserver)
                if not srv:
                    success = False
                    continue

                fd, mycnf_file = tempfile.mkstemp(prefix="frxbkp_", suffix=".cnf")
                cnf = f"[{section}]\npassword={srv['password']}\nhost={srv['host']}\n"
                if srv.get("port"):
                    cnf += f"port={srv['port']}\n"
                elif srv.get("socket"):
                    cnf += f"socket={srv['socket']}\n"
                os.write(fd, cnf.encode())
                os.close(fd)
                os.chmod(mycnf_file, 0o600)
                current_dbserver = dbserver

            dump_file = db_dir / f"{db['databasename']}.sql.gz"
            LOG.info("[%s] dumping database: %s → %s", customer["loginname"], db["databasename"], dump_file.name)

            dump_cmd = [
                mysqldump,
                f"--defaults-file={mycnf_file}",
                f"-u{srv['user']}",
                "--single-transaction",   # consistent InnoDB snapshot without table locks
                "--lock-tables=false",    # disable automatic LOCK TABLES for MyISAM
                "--quick",                # stream row-by-row, don't buffer entire result in RAM
                "--routines",
                "--events",
                db["databasename"],
            ]

            with open(dump_file, "wb") as f:
                p1 = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p2 = subprocess.Popen(["gzip", "-c"], stdin=p1.stdout, stdout=f, stderr=subprocess.PIPE)
                p1.stdout.close()
                _, gz_err = p2.communicate()
                _, dump_err = p1.communicate()

            if p1.returncode != 0:
                LOG.error("[%s] mysqldump failed for %s (rc=%d): %s",
                          customer["loginname"], db["databasename"], p1.returncode,
                          dump_err.decode(errors="replace")[:400])
                success = False
            else:
                size_mb = dump_file.stat().st_size / 1024 / 1024
                LOG.info("[%s] db backup: %s  %.1f MB", customer["loginname"], db["databasename"], size_mb)

    finally:
        if mycnf_file and os.path.exists(mycnf_file):
            os.unlink(mycnf_file)

    return success


# ─────────────────────────────────────────────────────────────
# Manifest
# ─────────────────────────────────────────────────────────────

def write_manifest(backup_dir: Path, info: dict, contents: list):
    manifest = {
        "version": 2,
        "tool": f"froxlor-backup {VERSION}",
        "timestamp": datetime.datetime.now().isoformat(),
        "domain": info.get("domain"),
        "customer": info.get("loginname"),
        "customerid": info.get("customerid"),
        "domain_id": info.get("domain_id"),
        "contents": contents,
    }
    with open(backup_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────
# Transfer to remote storage
# ─────────────────────────────────────────────────────────────

def transfer_rsync_ssh(local_dir: Path, remote_cfg: dict) -> bool:
    host = remote_cfg["host"]
    port = int(remote_cfg.get("port") or 22)
    user = remote_cfg["user"]
    key_file = remote_cfg.get("key_file")
    remote_path = remote_cfg["path"].rstrip("/")
    extra_args = remote_cfg.get("rsync_extra_args") or []

    ssh_opts = f"-p {port} -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=30"
    if key_file:
        ssh_opts += f" -i {key_file}"

    remote_dest = f"{user}@{host}:{remote_path}/"
    LOG.info("Transferring to %s (rsync+SSH) ...", remote_dest)

    cmd = [
        "rsync",
        "--archive",           # -rlptgoD
        "--compress",
        "--delete",            # remove old backups according to local retention
        "--partial",
        "--human-readable",
        "--timeout=120",
        "--chmod=D700,F600",   # enforce o=0 on remote regardless of remote umask
        f"--rsh=ssh {ssh_opts}",
    ] + extra_args + [
        str(local_dir) + "/",
        remote_dest,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        LOG.error("rsync failed (rc=%d): %s", result.returncode, result.stderr[:600])
        return False
    LOG.info("rsync done.")
    return True


def transfer_rclone(local_dir: Path, remote_cfg: dict) -> bool:
    rclone_remote = remote_cfg["rclone_remote"]
    remote_path = remote_cfg["path"].strip("/")
    config_file = remote_cfg.get("rclone_config")
    extra_args = remote_cfg.get("rclone_extra_args") or []

    dest = f"{rclone_remote}:{remote_path}"
    LOG.info("Transferring to %s (rclone) ...", dest)

    # S3/B2/GCS: objects are private by default (ACL-based, Unix perms don't apply).
    # SFTP: add "--sftp-umask=077" to rclone_extra_args in config.yaml to enforce o=0.
    cmd = ["rclone", "sync", "--progress", "--stats-one-line"]
    if config_file:
        cmd += ["--config", config_file]
    cmd += extra_args + [str(local_dir) + "/", dest]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        LOG.error("rclone failed (rc=%d): %s", result.returncode, result.stderr[:600])
        return False
    LOG.info("rclone done.")
    return True


def transfer_to_remotes(local_dir: Path, cfg: dict) -> bool:
    ok = True
    for remote in cfg.get("remotes") or []:
        if not remote.get("enabled", True):
            continue
        LOG.info("── Remote: %s (%s) ──", remote["name"], remote["type"])
        if remote["type"] == "rsync_ssh":
            ok = transfer_rsync_ssh(local_dir, remote) and ok
        elif remote["type"] == "rclone":
            ok = transfer_rclone(local_dir, remote) and ok
        else:
            LOG.warning("Unknown remote type: %s", remote["type"])
    return ok


# ─────────────────────────────────────────────────────────────
# Retention
# ─────────────────────────────────────────────────────────────

def apply_retention(backup_base: Path, cfg: dict):
    """
    Keeps:
      - the last retention.daily  daily backups
      - 1 backup/week for the last retention.weekly  weeks
      - 1 backup/month for the last retention.monthly months
    All other YYYY-MM-DD directories are deleted.
    Runs for every subdirectory of backup_base (= each domain / each customer DB dir).
    """
    ret = cfg.get("retention", {})
    keep_daily = int(ret.get("daily", 7))
    keep_weekly = int(ret.get("weekly", 4))
    keep_monthly = int(ret.get("monthly", 6))

    today = datetime.date.today()

    for entity_dir in sorted(backup_base.iterdir()):
        if not entity_dir.is_dir():
            continue

        # Collect date-stamped subdirectories: YYYY-MM-DD
        dated = []
        for sub in entity_dir.iterdir():
            if re.match(r"^\d{4}-\d{2}-\d{2}$", sub.name):
                try:
                    d = datetime.date.fromisoformat(sub.name)
                    dated.append((d, sub))
                except ValueError:
                    pass

        if not dated:
            continue

        dated.sort(reverse=True)  # newest first
        keep = set()

        # Daily: keep the last N
        for d, p in dated[:keep_daily]:
            keep.add(p)

        # Weekly: keep the newest entry from each of the last N weeks
        seen_weeks = {}
        for d, p in dated:
            iso_week = d.isocalendar()[:2]  # (year, week)
            if iso_week not in seen_weeks:
                seen_weeks[iso_week] = (d, p)
        weekly_sorted = sorted(seen_weeks.values(), reverse=True)
        for d, p in weekly_sorted[:keep_weekly]:
            keep.add(p)

        # Monthly: keep the newest entry from each of the last N months
        seen_months = {}
        for d, p in dated:
            ym = (d.year, d.month)
            if ym not in seen_months:
                seen_months[ym] = (d, p)
        monthly_sorted = sorted(seen_months.values(), reverse=True)
        for d, p in monthly_sorted[:keep_monthly]:
            keep.add(p)

        # Delete everything not in keep
        for d, p in dated:
            if p not in keep:
                LOG.info("Retention: removing old backup %s", p)
                shutil.rmtree(p)


# ─────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────

def send_notification(cfg: dict, subject: str, body: str):
    ntf = cfg.get("notifications", {})
    if not ntf.get("enabled"):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[froxlor-backup] {subject}"
        msg["From"] = ntf.get("email_from", "froxlor-backup@localhost")
        msg["To"] = ntf["email_to"]
        msg.set_content(body)

        host = ntf.get("smtp_host", "localhost")
        port = int(ntf.get("smtp_port", 25))
        # smtp_tls accepts three values:
        #   "ssl"      - direct TLS from the first byte (port 465 / SMTPS)
        #   "starttls" - plain connect, then upgrade via STARTTLS (port 587)
        #   false      - plain, no encryption (port 25, local relay)
        # Legacy bool true is treated as "ssl" for backwards compatibility.
        tls_mode = str(ntf.get("smtp_tls", "false")).lower()
        if tls_mode == "true":
            tls_mode = "ssl"

        import ssl as _ssl
        if tls_mode == "ssl":
            ctx = _ssl.create_default_context()
            smtp = smtplib.SMTP_SSL(host, port, context=ctx)
        elif tls_mode == "starttls":
            ctx = _ssl.create_default_context()
            smtp = smtplib.SMTP(host, port)
            smtp.starttls(context=ctx)
        else:
            smtp = smtplib.SMTP(host, port)

        if ntf.get("smtp_user"):
            smtp.login(ntf["smtp_user"], ntf["smtp_password"])
        smtp.send_message(msg)
        smtp.quit()
        LOG.debug("Notification email sent to %s", ntf["email_to"])
    except Exception as e:
        LOG.error("Failed to send notification email: %s", e)


# ─────────────────────────────────────────────────────────────
# Core backup logic
# ─────────────────────────────────────────────────────────────

def backup_domain(conn, domain: dict, backup_base: Path, cfg: dict, dry_run: bool) -> list:
    """Back up web + mail (+ optional logs) for one domain. Returns a list of errors."""
    date_str = datetime.date.today().isoformat()
    domain_slug = re.sub(r"[^a-zA-Z0-9._-]", "_", domain["domain"])
    backup_dir = backup_base / domain_slug / date_str
    errors = []

    if dry_run:
        LOG.info("[DRY-RUN] would back up domain %s → %s", domain["domain"], backup_dir)
        return errors

    backup_dir.mkdir(parents=True, exist_ok=True)
    contents = []

    if cfg["backup"].get("web", True):
        if backup_web(domain, backup_dir, cfg):
            contents.append("web")
        else:
            errors.append(f"{domain['domain']}: web backup failed")

    if cfg["backup"].get("mail", True) and domain.get("isemaildomain"):
        mail_accounts = get_domain_mail_accounts(conn, domain["domain_id"])
        if backup_mail(domain, mail_accounts, backup_dir):
            if mail_accounts:
                contents.append("mail")
        else:
            errors.append(f"{domain['domain']}: mail backup failed")

    if cfg["backup"].get("logs", False):
        if backup_logs(domain, backup_dir, cfg):
            contents.append("logs")

    write_manifest(backup_dir, domain, contents)

    if contents:
        LOG.info("[%s] backup complete: %s", domain["domain"], ", ".join(contents))
    else:
        LOG.warning("[%s] backup is empty (no files)", domain["domain"])
        shutil.rmtree(backup_dir, ignore_errors=True)

    return errors


def backup_customer_databases(conn, customer: dict, backup_base: Path, cfg: dict, dry_run: bool) -> list:
    """Back up all databases for a customer. Returns a list of errors."""
    date_str = datetime.date.today().isoformat()
    slug = f"_db_{customer['loginname']}"
    backup_dir = backup_base / slug / date_str
    errors = []

    if dry_run:
        LOG.info("[DRY-RUN] would back up databases for customer %s → %s", customer["loginname"], backup_dir)
        return errors

    backup_dir.mkdir(parents=True, exist_ok=True)

    if not backup_databases(conn, customer, backup_dir, cfg):
        errors.append(f"{customer['loginname']}: DB backup failed (partially or completely)")

    # Check if anything was actually written
    db_dir = backup_dir / "databases"
    if db_dir.exists() and any(db_dir.iterdir()):
        write_manifest(backup_dir, customer, ["databases"])
        LOG.info("[%s] DB backup complete", customer["loginname"])
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)

    return errors


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="froxlor-backup - automatic per-domain/per-customer backup"
    )
    p.add_argument("--config", default="/etc/froxlor-backup/config.yaml",
                   help="Path to the configuration file")
    p.add_argument("--domain",    help="Back up only this domain (e.g. example.com)")
    p.add_argument("--customer",  help="Back up only this customer (loginname)")
    p.add_argument("--skip-transfer", action="store_true",
                   help="Do not transfer to remote storage")
    p.add_argument("--skip-retention", action="store_true",
                   help="Do not apply retention policy")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be backed up without writing anything")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    # Strip all permissions from group and other for every file/dir created by this
    # process and its subprocesses (tar, gzip).  Dirs → 700, files → 600.
    # Must be set before the first mkdir/open call.
    os.umask(0o077)

    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)

    LOG.info("═══ froxlor-backup %s ═══", VERSION)
    if args.dry_run:
        LOG.info("MODE: DRY-RUN - no changes will be written to disk")

    backup_base = Path(cfg.get("local_backup_dir", "/var/backups/froxlor-backup"))
    backup_base.mkdir(parents=True, exist_ok=True)

    conn = db_connect(cfg)
    LOG.info("Connected to Froxlor DB.")

    domains = get_domains(conn, cfg)
    if args.domain:
        domains = [d for d in domains if d["domain"] == args.domain]
    if args.customer:
        domains = [d for d in domains if d["loginname"] == args.customer]

    LOG.info("Domains to back up: %d", len(domains))

    all_errors = []
    backed_customers = set()

    for domain in domains:
        try:
            errs = backup_domain(conn, domain, backup_base, cfg, args.dry_run)
            all_errors.extend(errs)
        except Exception as e:
            msg = f"{domain['domain']}: unexpected error - {e}"
            LOG.exception(msg)
            all_errors.append(msg)

        # Databases - once per customer
        if cfg["backup"].get("databases", True):
            cid = domain["customerid"]
            if cid not in backed_customers:
                backed_customers.add(cid)
                try:
                    errs = backup_customer_databases(conn, domain, backup_base, cfg, args.dry_run)
                    all_errors.extend(errs)
                except Exception as e:
                    msg = f"{domain['loginname']} (DB): unexpected error - {e}"
                    LOG.exception(msg)
                    all_errors.append(msg)

    conn.close()

    # Retention
    if not args.skip_retention and not args.dry_run:
        LOG.info("Applying retention policy ...")
        apply_retention(backup_base, cfg)

    # Transfer
    transfer_ok = True
    if not args.skip_transfer and not args.dry_run:
        LOG.info("Transferring to remote storage ...")
        transfer_ok = transfer_to_remotes(backup_base, cfg)
        if not transfer_ok:
            all_errors.append("Transfer to remote failed")

    # Summary
    if all_errors:
        summary = "Backup completed WITH ERRORS:\n" + "\n".join(f"  • {e}" for e in all_errors)
        LOG.error(summary)
        ntf_cfg = cfg.get("notifications", {})
        if ntf_cfg.get("on_error"):
            send_notification(cfg, "Backup ERROR", summary)
        sys.exit(1)
    else:
        LOG.info("Backup completed successfully.")
        ntf_cfg = cfg.get("notifications", {})
        if ntf_cfg.get("on_success"):
            send_notification(cfg, "Backup successful", f"Domains backed up: {len(domains)}")
        sys.exit(0)


if __name__ == "__main__":
    main()
