"""Mail Fetcher: poll an IMAP mailbox and relay each new message to another address.

The source mailbox is opened read-only, so nothing is marked read, moved or
deleted there. Progress is a UID high-water mark kept in a small JSON state file.
"""
import imaplib
import json
import logging
import os
import signal
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("mail-fetcher")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Config:
    imap_host: str
    imap_port: int
    imap_security: str
    imap_user: str
    imap_password: str
    imap_folder: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_user: str
    smtp_password: str
    smtp_from: str
    dest: str
    poll_seconds: int
    start_from: date | None
    data_dir: Path

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"


def load_config(env=os.environ) -> Config:
    def need(key):
        value = env.get(key, "").strip()
        if not value:
            raise SystemExit(f"Missing required setting: {key}")
        return value

    imap_user = need("IMAP_USER")
    start = env.get("START_FROM", "").strip()
    return Config(
        imap_host=need("IMAP_HOST"),
        imap_port=int(env.get("IMAP_PORT", "993")),
        imap_security=env.get("IMAP_SECURITY", "ssl").lower(),
        imap_user=imap_user,
        imap_password=need("IMAP_PASSWORD"),
        imap_folder=env.get("IMAP_FOLDER", "INBOX"),
        smtp_host=env.get("SMTP_HOST", "").strip() or need("IMAP_HOST"),
        smtp_port=int(env.get("SMTP_PORT", "465")),
        smtp_security=env.get("SMTP_SECURITY", "ssl").lower(),
        smtp_user=env.get("SMTP_USER", "").strip() or imap_user,
        smtp_password=env.get("SMTP_PASSWORD", "") or need("IMAP_PASSWORD"),
        smtp_from=env.get("SMTP_FROM", "").strip() or imap_user,
        dest=need("DEST_ADDRESS"),
        poll_seconds=int(env.get("POLL_SECONDS", "300")),
        start_from=date.fromisoformat(start) if start else None,
        data_dir=Path(env.get("DATA_DIR", "/data")),
    )


def imap_date(d: date) -> str:
    # strftime("%b") follows the locale; IMAP needs English month names.
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def connect_imap(cfg: Config) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if cfg.imap_security == "ssl":
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        if cfg.imap_security == "starttls":
            conn.starttls(ssl_context=ctx)
    conn.login(cfg.imap_user, cfg.imap_password)
    return conn


def connect_smtp(cfg: Config) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        conn = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=60)
    else:
        conn = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
        if cfg.smtp_security == "starttls":
            conn.starttls(context=ctx)
    conn.login(cfg.smtp_user, cfg.smtp_password)
    return conn


def load_state(cfg: Config) -> dict | None:
    try:
        return json.loads(cfg.state_file.read_text())
    except FileNotFoundError:
        return None


def save_state(cfg: Config, state: dict) -> None:
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(cfg.state_file)


def _uids(imap, *criteria) -> list[int]:
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"IMAP SEARCH failed: {criteria}")
    return sorted(int(u) for u in (data[0] or b"").split())


def initial_state(cfg: Config, imap, uidvalidity: int) -> dict:
    """Start at the first message on/after START_FROM (default: yesterday).

    SINCE is date-granular, so the start overlaps by design -- a duplicate is
    better than a hole.
    """
    since = cfg.start_from or (date.today() - timedelta(days=1))
    recent = _uids(imap, "SINCE", imap_date(since))
    if recent:
        last_uid = recent[0] - 1
    else:
        everything = _uids(imap, "ALL")
        last_uid = everything[-1] if everything else 0
    log.info("starting at UID %d (since %s)", last_uid + 1, since.isoformat())
    return {"uidvalidity": uidvalidity, "last_uid": last_uid, "rejected": []}


def relay(smtp: smtplib.SMTP, cfg: Config, raw: bytes) -> None:
    tag = f"X-Mail-Fetcher-Source: {cfg.imap_user}\r\n".encode()
    smtp.sendmail(cfg.smtp_from, [cfg.dest], tag + raw)


def is_permanent(exc: Exception) -> bool:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(code >= 500 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPResponseException):
        return 500 <= exc.smtp_code < 600
    return False


def poll_once(cfg: Config, connect_imap=connect_imap, connect_smtp=connect_smtp) -> int:
    """Relay every new message. Returns the number relayed."""
    imap = connect_imap(cfg)
    smtp = None
    sent = 0
    try:
        imap.select(cfg.imap_folder, readonly=True)
        uidvalidity = int(imap.response("UIDVALIDITY")[1][0])
        state = load_state(cfg)
        if state is None or state["uidvalidity"] != uidvalidity:
            if state is not None:
                log.warning("UIDVALIDITY changed; restarting from the start date")
            state = initial_state(cfg, imap, uidvalidity)
            save_state(cfg, state)

        # "N:*" always returns the newest message even if it is older than N.
        pending = [u for u in _uids(imap, "UID", f"{state['last_uid'] + 1}:*")
                   if u > state["last_uid"]]
        for uid in pending:
            typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                raise RuntimeError(f"IMAP FETCH failed for UID {uid}")
            raw = data[0][1]
            if smtp is None:
                smtp = connect_smtp(cfg)
            try:
                relay(smtp, cfg, raw)
                sent += 1
                log.info("relayed UID %d (%d bytes)", uid, len(raw))
            except Exception as exc:
                if not is_permanent(exc):
                    raise  # transient: stop here, retry next cycle, keep order
                log.error("UID %d permanently rejected, skipping: %s", uid, exc)
                state["rejected"].append(uid)
            state["last_uid"] = uid
            save_state(cfg, state)
    finally:
        for conn, close in ((smtp, "quit"), (imap, "logout")):
            if conn is not None:
                try:
                    getattr(conn, close)()
                except Exception:
                    pass
    return sent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("polling %s@%s every %ds -> %s", cfg.imap_user, cfg.imap_host,
             cfg.poll_seconds, cfg.dest)
    while not stop.is_set():
        try:
            poll_once(cfg)
            cfg.heartbeat_file.write_text(str(time.time()))
        except Exception as exc:
            log.error("poll failed: %s: %s", type(exc).__name__, exc)
        stop.wait(cfg.poll_seconds)


if __name__ == "__main__":
    main()
