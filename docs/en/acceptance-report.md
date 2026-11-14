# Test and Acceptance Report

[中文](../测试与验收报告.md) · **English**

This document records the **actual scope** of the project's automated test coverage, SQL capacity smoke test and real-Telegram acceptance, including what was verified and what was not. The report is kept in the open-source repository so that users can judge precisely what they may rely on.

Acceptance environment: Ubuntu 24.04 / Python 3.12 / python-telegram-bot 22.8 / PostgreSQL 16, a long-polling receiving service plus a separate persistent worker, no web dashboard. Demo bot: [@KunKunGuangGaoBot](https://t.me/KunKunGuangGaoBot).

## Architecture acceptance

Both systemd services start at boot and restart automatically on failure, and an SQL advisory lock prevents two instances under the same role:

| Service | Responsibility | Started by |
| --- | --- | --- |
| `tg-auto-ad-bot.service` | Persist updates, user interaction, source ingestion | `python -m adbot.main` |
| `tg-auto-ad-worker.service` | Rotation, instant events, per-target sends, broadcast progress | `python -m adbot.worker` |

PostgreSQL listens on the local host only and is not exposed publicly; the receiving service listens on no web port at all. A live process does not mean the business is healthy — **the send path can produce `ConnectTimeout` or a TLS `Hostname mismatch` while the process is still alive**, so the project ships a health-check script that judges real availability from the receiving/worker heartbeats instead of looking at `systemctl status` alone.

## Automated tests

```text
195 passed, 10 warnings in 43.18s
```

| Test module | Count | Key coverage |
| --- | ---: | --- |
| `tests/test_content.py` | 133 | Link and interval parsing, button syntax, UTF-16 entity offsets, the five media types, copy/forward, icon discovery, explicit eligibility downgrade, no resend on network error |
| `tests/test_ui.py` | 24 | Ownership isolation, admin checks, bans, session confirmation, private-target picker, menu icon downgrade, shared rate limiting |
| `tests/test_reliability.py` | 30 | Real-SQL state machines, partial failure, `UNKNOWN` and recovery, pause/stop races, delete-old then post-new, source events, broadcasts, cross-connection rate limiting |
| `tests/test_ui_sql.py` | 6 | Full interaction flows against real SQL, concurrent content-version confirmation, task toggles, search/config/ban, broadcast confirmation and progress |
| `tests/test_scale.py` | 2 | Scheduling integrity and rotation fairness at 2,000 users / 10,000 bindings / 2,000 tasks |

(The table shows the module distribution at first release; for the billing module tests added later see the [Billing report](billing-report.md) — the same command now reports 481 passed for the full suite.)

All 10 warnings come from python-telegram-bot deprecation notices about `retry_after` becoming a `timedelta` in the future; the implementation accepts both a number and a `timedelta`, so results are unaffected.

Every automated SQL test uses a dedicated test database, a random schema and a fake `Bot` object: no real token is read, nothing is sent to real users, and no network is used. No schema is left behind after the run.

## Local capacity smoke test

Generated in an isolated SQL environment: 2,000 users, 10,000 chats and bindings, 2,000 running tasks (5 targets per task):

| Measurement | Result | Notes |
| --- | --- | --- |
| 20 scheduling batches (100 tasks each) | 6.662 s | Produced 2,000 events and 10,000 delivery records, with no missing relation and no duplicate scheduling |
| Size of that schema | about 9.92 MiB | Includes join tables, TOAST and indexes; this sample only, not a long-term space forecast |
| New-task scheduling with 10,000 future retry records | 0.097 s | Ready tasks are still served on the first tick |
| Rotation after all 2,000 tasks recover | 63 ticks / 9.559 s | Every task is served at least once within 63 ticks |

These numbers skip real network waits and prove only **SQL integrity, scheduling fairness and index design**; they are **not** a real-Telegram load test with ten thousand users, a long-run stability test or a throughput SLA.

## Real Telegram acceptance scope

Real sends were limited to the private chats of two configured admins, with no batch testing against production users, channels or groups:

1. Main menu: each admin received 9 buttons and 9 button icons, and the structure returned by Telegram matched expectations.
2. A message with a custom emoji and three colored buttons: Telegram returned 1 `custom_emoji` body entity and 3 buttons, with `primary` / `success` / `danger` all preserved and `degraded=false`.
3. Real rotation send from the worker on an isolated database: two consecutive rounds; before the second round `deleteMessage` removed the first-round message, SQL recorded `SEND SUCCESS` / `DELETE SUCCESS` / `SEND SUCCESS` in order, and the task moved to `COMPLETED` after the configured repeat count.
4. Single-recipient broadcast: `total=1, success=1, failed=0, unknown=0`, status `COMPLETED`, and the progress message was successfully edited by the worker.
5. Menu icons come from the real HandEmoji icon set: seeded from the official examples and verified in bulk with `getCustomEmojiStickers` before being written to SQL — 9 categories, no fabricated IDs.

**What was not verified** (do not assume it is accepted):

- Real admin-permission combinations in channels / groups and every media scenario — bind a test target yourself and accept it with the **Test** button as described in the [Usage guide](usage.md).
- Cross-client visual differences (animation, custom-emoji rendering) depend on the Telegram client; an entity returned by the API does not mean every client displays it identically.
- A real broadcast to ten thousand users, and long-run stability load testing.

The worker private-chat diagnostic script runs only in a random schema and replaced the target check only for that known admin's private chat; production permission logic is unchanged.

## Startup behaviour

On startup the receiving service tries to send each admin one `已启动` (started) notification containing the receiving service status and the `/start` and `/admin` entry points. **Startup creates no ad task and broadcasts to nobody**: tasks and broadcasts already confirmed for launch continue from SQL state, and uncertain requests left behind become `UNKNOWN` awaiting manual review.

## Data, health and backup snapshot

Production snapshot after the first release had stabilised (the magnitude of these metrics is a useful reference for small deployments):

- Health check `healthy=true`, receiving heartbeat 4.0 s, worker heartbeat 0.0 s.
- Users 2 (the two admins), production tasks 0, production target bindings 0, production broadcasts 0.
- Pending updates 0, errored updates 0, unknown task deliveries 0, unknown broadcast recipients 0.
- Production database about 8.56 MiB; migrations `001_init.sql`, `002_scheduler.sql` and `003_shared_rate.sql` all applied.
- `.env` mode 600; `data` / `logs` / `backups` directories 700; backup files 600.

The backup tool takes a consistent snapshot and verifies the catalogue with `pg_restore --list`; the restore tool **refuses** to overwrite the database referenced by the current `DATABASE_URL` or any non-empty database. On the first release a backup was restored into a separate empty database, verifying 22 tables, 3 migrations and 8 configuration entries without touching production. The project ships **no** automatic backup schedule; hook it into your own cron or monitoring system.

## Known boundaries

- Free sends are bound by Telegram rate limits: about 20 per second globally and about a 3.1-second gap per group. For 10,000 targets the ideal global send floor alone is about 8 minutes 20 seconds, and permission lookups, delete-old, media and the network add more; **"a 1-minute minimum interval" does not mean 10,000 targets finish in one minute**.
- One "completion" of a task means one selected item was delivered to this batch of targets, after which the cursor moves to the next item; a full A→B→C loop counts as three. Instant events do not increment that counter.
- When a multi-target send partially fails, targets that succeeded are never resent automatically; an `UNKNOWN` needs manual review, then either a resend (may duplicate) or a skip (may miss). The Bot API has no business idempotency key, so **absolute exactly-once behaviour towards the outside world is not promised**.
- Custom-emoji eligibility is decided per actual send scenario; acceptance in a private chat does not prove a channel is also allowed. Only an explicit eligibility rejection triggers a downgrade — an unknown network error is never disguised as success and sent again.
- The Bot API cannot enumerate arbitrary history, and limits such as protected content cannot be bypassed; forwarded messages do not support custom buttons.
- This version handles one media item at a time; albums are a later extension.
