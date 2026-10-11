import smtplib
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from app import main


class FakeImap:
    def __init__(self, messages, uidvalidity=1):
        self.messages = messages  # {uid: raw bytes}
        self.uidvalidity = uidvalidity

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def response(self, name):
        return name, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[1:]
            uids = sorted(self.messages)
            if criteria[0] == "UID":
                low = int(criteria[1].split(":")[0])
                uids = [u for u in uids if u >= low] or uids[-1:]
            elif criteria[0] == "SINCE":
                uids = [u for u in uids if self.messages[u].startswith(b"new")]
            return "OK", [b" ".join(str(u).encode() for u in uids)]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {n}", self.messages[int(args[0])]), b")"]
        raise AssertionError(command)

    def logout(self):
        pass


class FakeSmtp:
    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    def sendmail(self, sender, recipients, data):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((sender, recipients, data))

    def quit(self):
        pass


def make_cfg(tmp):
    return main.Config(
        imap_host="h", imap_port=993, imap_security="ssl", imap_auth="plain",
        imap_user="me@example.com", imap_password="x", imap_cert="", imap_key="", imap_folder="INBOX", smtp_host="h", smtp_port=465,
        smtp_security="ssl", smtp_auth="plain", smtp_user="me@example.com",
        smtp_password="x", smtp_cert="", smtp_key="", smtp_from="me@example.com", dest="dest@proton.me", poll_seconds=1,
        rewrite_from=True, use_idle=True, idle_seconds=1,
        start_from=date(2026, 10, 10), data_dir=Path(tmp),
    )


class RewriteTests(unittest.TestCase):
    RAW = (b"Return-Path: <a@gmail.com>\r\nDelivered-To: me@bytz.me\r\n"
           b"DKIM-Signature: v=1; d=gmail.com;\r\n\tb=abc\r\n"
           b"From: Brett Craig <a@gmail.com>\r\nTo: me@bytz.me\r\nSubject: hi\r\n"
           b"Content-Type: text/plain\r\n\r\nbody\r\n\r\nmore\r\n")

    def test_rewrites_from_keeps_body_and_sets_reply_to(self):
        out = main.rewrite_from(self.RAW, "me@braxmail.net")
        head, body = out.split(b"\r\n\r\n", 1)
        self.assertEqual(body, b"body\r\n\r\nmore\r\n")
        self.assertIn(b'From: "Brett Craig (a@gmail.com) via Mail Fetcher" <me@braxmail.net>', head)
        self.assertIn(b"Reply-To: Brett Craig <a@gmail.com>", head)
        self.assertIn(b"X-Original-From: Brett Craig <a@gmail.com>", head)
        self.assertIn(b"Subject: hi", head)
        for gone in (b"Return-Path", b"Delivered-To", b"DKIM-Signature", b"b=abc"):
            self.assertNotIn(gone, head)

    def test_existing_reply_to_is_kept_and_own_mail_is_untouched(self):
        raw = b"From: a@gmail.com\nReply-To: list@x.org\nSubject: s\n\nbody\n"
        out = main.rewrite_from(raw, "me@braxmail.net")
        self.assertEqual(out.count(b"Reply-To"), 1)
        self.assertIn(b"Reply-To: list@x.org", out)
        self.assertIn(b"\n\nbody\n", out)
        mine = b"From: me@braxmail.net\r\nSubject: s\r\n\r\nbody\r\n"
        self.assertEqual(main.rewrite_from(mine, "me@braxmail.net"), mine)


class FakeIdleImap:
    """Just enough of imaplib for wait_for_mail: scripted server output."""

    def __init__(self, chunks, uidnext=7, capabilities=("IMAP4REV1", "IDLE")):
        self.capabilities = capabilities
        self.chunks = list(chunks)
        self.uidnext = uidnext
        self.sent = []
        imap = self

        class Sock:
            def settimeout(self, t):
                pass

            def recv(self, n):
                if b"DONE\r\n" in imap.sent:
                    return b"A1 OK IDLE terminated\r\n"  # server answers only after DONE
                if not imap.chunks:
                    raise TimeoutError
                return imap.chunks.pop(0)

        self.sock = Sock()

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def response(self, name):
        return name, [str(self.uidnext).encode()]

    def _new_tag(self):
        return b"A1"

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        return b"+ idling\r\n"

    def logout(self):
        pass


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(self.tmp.name)
        main.save_state(self.cfg, {"uidvalidity": 1, "last_uid": 6, "rejected": []})

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_on_exists_and_ends_idle(self):
        imap = FakeIdleImap([b"* 9 EXISTS\r\n"])
        main.wait_for_mail(self.cfg, threading.Event(), lambda _: imap)
        self.assertEqual(imap.sent, [b"A1 IDLE\r\n", b"DONE\r\n"])

    def test_returns_without_idling_when_mail_already_waiting(self):
        imap = FakeIdleImap([], uidnext=8)  # UID 7 exists but last_uid is 6
        main.wait_for_mail(self.cfg, threading.Event(), lambda _: imap)
        self.assertEqual(imap.sent, [])

    def test_idle_renews_after_timeout(self):
        imap = FakeIdleImap([])
        main.wait_for_mail(self.cfg, threading.Event(), lambda _: imap)
        self.assertEqual(imap.sent, [b"A1 IDLE\r\n", b"DONE\r\n"])


class PollTests(unittest.TestCase):
    def run_poll(self, cfg, imap, smtp):
        return main.poll_once(cfg, lambda _: imap, lambda _: smtp)

    def test_first_run_skips_old_mail_then_relays_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            imap = FakeImap({1: b"old a", 2: b"old b", 3: b"new c", 4: b"new d"})
            smtp = FakeSmtp()
            self.assertEqual(self.run_poll(cfg, imap, smtp), 2)
            self.assertEqual([m[2].split(b"\r\n", 1)[1] for m in smtp.sent], [b"new c", b"new d"])
            self.assertIn(b"X-Mail-Fetcher-Source: me@example.com", smtp.sent[0][2])
            # Nothing new on the next poll, even though "N:*" returns the last message.
            self.assertEqual(self.run_poll(cfg, imap, FakeSmtp()), 0)
            imap.messages[5] = b"new e"
            self.assertEqual(self.run_poll(cfg, imap, FakeSmtp()), 1)

    def test_transient_failure_keeps_message_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            imap = FakeImap({1: b"new a"})
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                self.run_poll(cfg, imap, FakeSmtp(smtplib.SMTPServerDisconnected("down")))
            self.assertEqual(self.run_poll(cfg, imap, FakeSmtp()), 1)

    def test_permanent_rejection_is_skipped_not_retried_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            imap = FakeImap({1: b"new a", 2: b"new b"})
            bad = smtplib.SMTPDataError(554, b"rejected")
            self.assertEqual(self.run_poll(cfg, imap, FakeSmtp(bad)), 0)
            self.assertEqual(main.load_state(cfg)["rejected"], [1, 2])
            self.assertEqual(self.run_poll(cfg, imap, FakeSmtp()), 0)

    def test_config_accepts_k9_style_values_and_defaults_ports(self):
        env = {"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "DEST_ADDRESS": "d",
               "IMAP_SECURITY": "SSL/TLS", "SMTP_SECURITY": "STARTTLS",
               "SMTP_FROM": "u@example.com"}
        cfg = main.load_config(env)
        self.assertEqual((cfg.imap_security, cfg.imap_port), ("ssl", 993))
        self.assertEqual((cfg.smtp_security, cfg.smtp_port), ("starttls", 587))
        with self.assertRaises(SystemExit):
            main.load_config({**env, "IMAP_AUTH": "ntlm"})
        with self.assertRaises(SystemExit):  # bare username can't be the envelope sender
            main.load_config({k: v for k, v in env.items() if k != "SMTP_FROM"})
        main.load_config({**env, "SMTP_FROM": "u@example.com"})

    def test_imap_date_ignores_locale(self):
        self.assertEqual(main.imap_date(date(2026, 10, 3)), "03-Oct-2026")


if __name__ == "__main__":
    unittest.main()
