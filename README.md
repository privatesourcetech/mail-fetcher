# Mail Fetcher

A tiny self-hosted container that polls an IMAP mailbox and relays every new
message, unchanged, to another address over SMTP. Built for mailboxes whose
provider offers no forwarding rule.

## How it works

- Opens the source mailbox **read-only**: nothing is marked read, moved or deleted,
  so your normal mail app keeps working.
- Remembers the last relayed IMAP UID in `/data/state.json`. On the first run it
  starts at the first message on/after `START_FROM` (default: yesterday), so it
  never replays the whole mailbox. The start date overlaps on purpose -- a
  duplicate is better than a hole.
- Prepends an `X-Mail-Fetcher-Source` header and leaves `To`, `Subject`, `Message-ID`
  and the body intact, so the receiving side can filter or label by source.
- **Rewrites the visible `From`** (`REWRITE_FROM`, on by default) to the mailbox
  address, shown as "Original Name (original@address) via Mail Fetcher", with the real sender kept in
  `Reply-To` and `X-Original-From`. Without this, destinations that enforce
  DMARC/anti-spoofing (Proton does) silently drop mail whose `From` domain did not
  send it. Original DKIM/ARC/Authentication-Results headers are removed because
  they no longer verify. The body is not touched.
- Transient failures (network, 4xx) stop the cycle and retry in order. A
  permanent rejection (5xx) is logged, recorded under `rejected` in the state file
  and skipped so one bad message can't block the queue.
- Python standard library only.

## Configuration

All settings are environment variables; see `.env.example`. Credentials go in an
untracked `.env`, never in `compose.yaml`.

Incoming and outgoing servers each take host, security (`ssl`/`starttls`/`none`),
port, authentication (`plain` or `cram-md5`; SMTP also `none`), username,
password and an optional client certificate (paths to files mounted in the
container). Not supported: NTLM and OAuth2 authentication, IMAP compression,
and sending client information -- none are needed to relay mail. The IMAP
namespace is not auto-detected; set `IMAP_FOLDER` if the mailbox isn't `INBOX`.

## Deploying (TrueNAS)

1. Create a dataset for the app in the TrueNAS UI.
2. Put `compose.yaml` (from `compose.example.yaml`), `.env` and an empty `data/`
   folder in it.
3. Create the app with **Install via YAML**. The image is built by
   `.github/workflows/publish-image.yml` and published to GHCR on push to `main`.

## Tests

    python -m unittest discover tests

## Limits

- One mailbox and one destination per container. Run another instance for more.
- Uses IMAP IDLE to learn about new mail within seconds when the server supports it
  (`USE_IDLE`, on by default); otherwise, and after any error, it falls back to
  polling every `POLL_SECONDS` (default 300). IDLE is only a wake-up hint -- the
  poll still does the actual relaying.
- Relayed mail is sent through your provider's SMTP, so its sending limits and
  spam handling apply.
