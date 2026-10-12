"""Mail Fetcher: poll an IMAP mailbox and relay each new message to another address.

The source mailbox is opened read-only, so nothing is marked read, moved or
deleted there. Progress is a UID high-water mark kept in a small JSON state file.
"""
import imaplib
import json
import logging
import os
import re
import signal
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.utils import formataddr, parseaddr
from pathlib import Path

log = logging.getLogger("mail-fetcher")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Config:
    imap_host: str
    imap_port: int
    imap_security: str
    imap_auth: str
    imap_user: str
    imap_password: str
    imap_cert: str
    imap_key: str
    imap_folder: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_auth: str
    smtp_user: str
    smtp_password: str
    smtp_cert: str
    smtp_key: str
    smtp_from: str
    dest: str
    poll_seconds: int
    rewrite_from: bool
    use_idle: bool
    idle_seconds: int
    start_from: date | None
    data_dir: Path

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"


_SECURITY = {"ssl": "ssl", "tls": "ssl", "ssl/tls": "ssl", "starttls": "starttls", "none": "none"}
_AUTH = {"plain": "plain", "password": "plain", "cram-md5": "cram-md5", "none": "none"}


def _choice(env, key, default, table):
    value = env.get(key, default).strip().lower()
    if value not in table:
        raise SystemExit(f"{key} must be one of: {', '.join(sorted(table))} (got {value!r})")
    return table[value]


def load_config(env=os.environ) -> Config:
    def need(key):
        value = env.get(key, "").strip()
        if not value:
            raise SystemExit(f"Missing required setting: {key}")
        return value

    imap_user = need("IMAP_USER")
    smtp_from = env.get("SMTP_FROM", "").strip() or imap_user
    if "@" not in smtp_from:
        raise SystemExit("SMTP_FROM (or IMAP_USER if unset) must be a full email address, "
                         "e.g. you@example.com -- a bare username makes the SMTP server "
                         "accept mail that the destination then rejects")
    start = env.get("START_FROM", "").strip()
    imap_security = _choice(env, "IMAP_SECURITY", "ssl", _SECURITY)
    smtp_security = _choice(env, "SMTP_SECURITY", "ssl", _SECURITY)
    smtp_auth = _choice(env, "SMTP_AUTH", "plain", _AUTH)
    imap_auth = _choice(env, "IMAP_AUTH", "plain", _AUTH)
    if imap_auth == "none":
        raise SystemExit("IMAP_AUTH cannot be none")
    return Config(
        imap_host=need("IMAP_HOST"),
        imap_port=int(env.get("IMAP_PORT", "993" if imap_security == "ssl" else "143")),
        imap_security=imap_security,
        imap_auth=imap_auth,
        imap_user=imap_user,
        imap_password=need("IMAP_PASSWORD"),
        imap_cert=env.get("IMAP_CLIENT_CERT", "").strip(),
        imap_key=env.get("IMAP_CLIENT_KEY", "").strip(),
        imap_folder=env.get("IMAP_FOLDER", "INBOX"),
        smtp_host=env.get("SMTP_HOST", "").strip() or need("IMAP_HOST"),
        smtp_port=int(env.get("SMTP_PORT", {"ssl": "465", "starttls": "587"}.get(smtp_security, "25"))),
        smtp_security=smtp_security,
        smtp_auth=smtp_auth,
        smtp_user=env.get("SMTP_USER", "").strip() or imap_user,
        smtp_password=env.get("SMTP_PASSWORD", "") or need("IMAP_PASSWORD"),
        smtp_cert=env.get("SMTP_CLIENT_CERT", "").strip(),
        smtp_key=env.get("SMTP_CLIENT_KEY", "").strip(),
        smtp_from=smtp_from,
        dest=need("DEST_ADDRESS"),
        poll_seconds=int(env.get("POLL_SECONDS", "300")),
        rewrite_from=env.get("REWRITE_FROM", "true").strip().lower() not in ("0", "false", "no", "off"),
        use_idle=env.get("USE_IDLE", "true").strip().lower() not in ("0", "false", "no", "off"),
        idle_seconds=int(env.get("IDLE_SECONDS", "300")),
        start_from=date.fromisoformat(start) if start else None,
        data_dir=Path(env.get("DATA_DIR", "/data")),
    )


def imap_date(d: date) -> str:
    # strftime("%b") follows the locale; IMAP needs English month names.
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if cert:
        ctx.load_cert_chain(cert, key or None)
    return ctx


def connect_imap(cfg: Config) -> imaplib.IMAP4:
    ctx = tls_context(cfg.imap_cert, cfg.imap_key)
    if cfg.imap_security == "ssl":
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        if cfg.imap_security == "starttls":
            conn.starttls(ssl_context=ctx)
    if cfg.imap_auth == "cram-md5":
        conn.login_cram_md5(cfg.imap_user, cfg.imap_password)
    else:
        conn.login(cfg.imap_user, cfg.imap_password)
    return conn


def connect_smtp(cfg: Config) -> smtplib.SMTP:
    ctx = tls_context(cfg.smtp_cert, cfg.smtp_key)
    if cfg.smtp_security == "ssl":
        conn = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=60)
    else:
        conn = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
        if cfg.smtp_security == "starttls":
            conn.starttls(context=ctx)
    if cfg.smtp_auth == "cram-md5":
        conn.user, conn.password = cfg.smtp_user, cfg.smtp_password  # read by auth_cram_md5
        conn.auth("CRAM-MD5", conn.auth_cram_md5)
    elif cfg.smtp_auth == "plain":
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


# Headers that stop being true (or would fail verification) once the message is
# re-sent from this server with a different From.
_STRIP = re.compile(rb"^(return-path|delivered-to|dkim-signature|authentication-results|"
                    rb"received-spf|arc-[a-z-]+|x-original-from)$", re.I)


def split_headers(raw: bytes) -> tuple[list[bytes], bytes, bytes]:
    """Return (logical header fields incl. folded lines, line ending, body incl. blank line)."""
    m = re.search(rb"\r?\n\r?\n", raw)
    head, sep_body = (raw[:m.start()], raw[m.start():]) if m else (raw, b"")
    eol = b"\r\n" if b"\r\n" in head or sep_body.startswith(b"\r\n") else b"\n"
    fields: list[bytes] = []
    for line in head.splitlines(keepends=True):
        if line[:1] in b" \t" and fields:
            fields[-1] += line
        else:
            fields.append(line)
    fields = [f.rstrip(b"\r\n") for f in fields]
    return fields, eol, sep_body


def prepare(raw: bytes, new_addr: str, rewrite: bool = True) -> bytes:
    """Make a fetched message safe to send again from this server.

    Always drops headers that cannot be valid on the re-sent copy -- above all
    DKIM-Signature: the relaying server adds its own, and some downstream hops
    (Amazon SES, for one) reject a message with two ("Duplicate header
    'DKIM-Signature'"). If the visible sender is not this mailbox, also make it
    this mailbox so the relay's sender checks pass; the real sender moves to
    Reply-To / X-Original-From. Only the header block is touched, so MIME
    bodies pass through byte for byte.
    """
    fields, eol, rest = split_headers(raw)
    from_field = next((f for f in fields if f.split(b":", 1)[0].strip().lower() == b"from"), None)
    orig_from = name = addr = None
    if from_field is not None:
        orig_from = " ".join(from_field.split(b":", 1)[1].decode("utf-8", "replace").split())
        name, addr = parseaddr(str(make_header(decode_header(orig_from))))
    do_rewrite = rewrite and from_field is not None and (addr or "").lower() != new_addr.lower()

    kept, has_reply_to = [], False
    for f in fields:
        hname = f.split(b":", 1)[0].strip()
        lname = hname.lower()
        if lname == b"from" and do_rewrite:
            continue
        if lname == b"reply-to":
            has_reply_to = True
        if _STRIP.match(hname):
            continue
        kept.append(f)
    if not do_rewrite:
        return eol.join(kept) + rest
    # Put the real address in the display name: the From address is this mailbox,
    # so the destination otherwise never shows who actually wrote.
    display = f"{name} ({addr}) via Mail Fetcher" if name and addr else f"{name or addr} via Mail Fetcher"
    new = [f"From: {formataddr((display, new_addr), charset='utf-8')}".encode(),
           f"X-Original-From: {orig_from}".encode()]
    if not has_reply_to and addr:
        new.append(f"Reply-To: {orig_from}".encode())
    return eol.join(new + kept) + rest


def relay(smtp: smtplib.SMTP, cfg: Config, raw: bytes) -> None:
    raw = prepare(raw, cfg.smtp_from, cfg.rewrite_from)
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


def wait_for_mail(cfg: Config, stop: threading.Event, connect=connect_imap) -> None:
    """Block until the server announces new mail (IMAP IDLE), IDLE_SECONDS pass or stop is set.

    Only a wake-up hint: poll_once() does the real work. Falls back to a plain
    sleep when the server has no IDLE.
    """
    imap = connect(cfg)
    try:
        if "IDLE" not in imap.capabilities:
            stop.wait(cfg.poll_seconds)
            return
        imap.select(cfg.imap_folder, readonly=True)
        state = load_state(cfg)
        uidnext = int(imap.response("UIDNEXT")[1][0])
        if state is None or uidnext - 1 > state["last_uid"]:
            return  # mail arrived between the poll and now
        tag = imap._new_tag()
        imap.send(tag + b" IDLE\r\n")
        if not imap.readline().startswith(b"+"):
            stop.wait(cfg.poll_seconds)
            return
        # Read the socket directly: after a timeout, imaplib's buffered file
        # object refuses all further reads ("cannot read from timed out object").
        sock = imap.sock
        sock.settimeout(5)
        pending = b""
        deadline = time.monotonic() + cfg.idle_seconds
        while not stop.is_set() and time.monotonic() < deadline and b"EXISTS" not in pending:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                raise ConnectionError("server closed the connection during IDLE")
            pending += chunk
        imap.send(b"DONE\r\n")
        sock.settimeout(30)
        while tag + b" " not in pending:
            chunk = sock.recv(4096)
            if not chunk:
                break
            pending += chunk
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("watching %s@%s -> %s (%s)", cfg.imap_user, cfg.imap_host, cfg.dest,
             f"IMAP IDLE, renewed every {cfg.idle_seconds}s" if cfg.use_idle
             else f"polling every {cfg.poll_seconds}s")
    while not stop.is_set():
        healthy = True
        try:
            poll_once(cfg)
            cfg.heartbeat_file.write_text(str(time.time()))
        except Exception as exc:
            healthy = False
            log.error("poll failed: %s: %s", type(exc).__name__, exc)
        if healthy and cfg.use_idle:
            try:
                wait_for_mail(cfg, stop)
                continue
            except Exception as exc:
                log.warning("IDLE failed, falling back to a timed wait: %s: %s",
                            type(exc).__name__, exc)
        stop.wait(cfg.poll_seconds)


if __name__ == "__main__":
    main()
