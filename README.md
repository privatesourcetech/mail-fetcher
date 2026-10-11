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
- Prepends an `X-Mail-Fetcher-Source` header and leaves the original headers
  (including `To`) intact, so the receiving side can filter or label by source.
- Transient failures (network, 4xx) stop the cycle and retry in order. A
  permanent rejection (5xx) is logged, recorded under `rejected` in the state file
  and skipped so one bad message can't block the queue.
- Python standard library only.

## Configuration

All settings are environment variables; see `.env.example`. Credentials go in an
untracked `.env`, never in `compose.yaml`.

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
- Polls on an interval (`POLL_SECONDS`, default 300); it does not use IMAP IDLE.
- Relayed mail is sent through your provider's SMTP, so its sending limits and
  spam handling apply.
