import smtplib
import tempfile
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
        start_from=date(2026, 10, 10), data_dir=Path(tmp),
    )


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
               "IMAP_SECURITY": "SSL/TLS", "SMTP_SECURITY": "STARTTLS"}
        cfg = main.load_config(env)
        self.assertEqual((cfg.imap_security, cfg.imap_port), ("ssl", 993))
        self.assertEqual((cfg.smtp_security, cfg.smtp_port), ("starttls", 587))
        with self.assertRaises(SystemExit):
            main.load_config({**env, "IMAP_AUTH": "ntlm"})

    def test_imap_date_ignores_locale(self):
        self.assertEqual(main.imap_date(date(2026, 10, 3)), "03-Oct-2026")


if __name__ == "__main__":
    unittest.main()
