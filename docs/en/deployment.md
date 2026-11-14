# Deployment and Maintenance

[中文](../部署与维护.md) · **English**

Project: `/opt/tg-auto-ad`. Bot: @KunKunGuangGaoBot. All business and management happen in a Telegram private chat; there is no web dashboard. The program uses official Bot API long polling, with a separate Worker and a persistent PostgreSQL 16 queue.

## Environment and files

- Ubuntu 24.04, Python 3.12, a dedicated `.venv`; dependencies are pinned in `requirements.txt`.
- A dedicated system account and database role `adbot`, production database `auto_ad`, authenticated over the UNIX socket with peer auth. Neither the test database `auto_ad_test` nor the backup-restore verification database `auto_ad_restore_check` takes part in production.
- `.env` is owned by `adbot:adbot` with mode 600; `.env.example` contains no real token. Restart both services after changing the token, and never write secrets into tickets, logs or public archives.
- Source and migration files are 644 and directories 755; the `data`, `logs` and `backups` directories are 700. The database accepts local connections only, so no public database port has to be opened.
- `migrations` are applied in filename order and executed records are kept in `schema_migrations`. Never modify a migration that has already shipped; add a new migration file instead.

## Installing on a new server

The steps below are for a fresh Ubuntu 24.04. This machine is already installed — do not re-initialise the production database. Copy the source into the same directory first, then install the dependencies:

```bash
apt-get update
apt-get install -y postgresql postgresql-contrib python3-venv
useradd --system --home-dir /opt/tg-auto-ad --no-create-home --shell /usr/sbin/nologin adbot
cd /opt/tg-auto-ad
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
runuser -u postgres -- createuser --login adbot
runuser -u postgres -- createdb --owner=adbot auto_ad
runuser -u postgres -- psql -c "ALTER ROLE adbot SET timezone TO 'UTC';"
install -o adbot -g adbot -m 600 .env.example .env
install -d -o adbot -g adbot -m 700 data logs backups
```

Fill the real token into `.env` with a controlled editor and check the admin IDs and DSN; peer authentication over the local socket requires the OS user and the SQL role to have the same name. Tests need a separate empty `auto_ad_test` database owned by adbot, and production never depends on the test database. adbot must be able to read the source and migrations (files 644, directories 755).

```bash
install -m 644 deploy/tg-auto-ad-bot.service /etc/systemd/system/tg-auto-ad-bot.service
install -m 644 deploy/tg-auto-ad-worker.service /etc/systemd/system/tg-auto-ad-worker.service
systemctl daemon-reload
systemctl enable --now tg-auto-ad-worker.service tg-auto-ad-bot.service
```

Migrations run automatically on first start. No web service has to be installed or started, and there is no dependency on Redis or MySQL. If you change the install directory, update `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, `ReadWritePaths` and the virtualenv path in both units.

## Service management

```bash
systemctl status tg-auto-ad-bot.service tg-auto-ad-worker.service
systemctl restart tg-auto-ad-bot.service tg-auto-ad-worker.service
systemctl stop tg-auto-ad-bot.service tg-auto-ad-worker.service
journalctl -u tg-auto-ad-bot.service -u tg-auto-ad-worker.service -n 100 --no-pager
journalctl -u tg-auto-ad-worker.service -f
```

Both services start at boot and restart automatically on failure; a PostgreSQL advisory lock prevents two instances under the same role. The receiving service persists updates and handles user interaction; the Worker handles rotation, instant sources, broadcasts and test deletions. A new task defaults to a draft and must be confirmed for launch after saving. An unknown network result is never resent automatically — verify it in the task or broadcast detail.

On every start the receiving service tries to send one startup notification to each of the admins 100000001 and 100000002; it never sends ads automatically to all users, channels or groups. Tasks and broadcasts explicitly launched continue from database state; requests left in flight become `UNKNOWN`. If the Worker raises an error it tries to notify the admins, and after repeated abnormal exits systemd restarts it.

## Health checks

```bash
cd /opt/tg-auto-ad
runuser -u adbot -- .venv/bin/python scripts/health.py
```

It prints the receiving and Worker heartbeats, pending and errored updates, the `UNKNOWN` count, running tasks, database size and migrations. It exits 0 when the receiving heartbeat is under 100 seconds and the Worker heartbeat under 120 seconds, otherwise 1. The first long poll after startup, a long 429 cooldown or a large batch can cause a temporary timeout, so judge together with the journal and the queue. The tool creates no scheduled jobs or external monitoring alerts; feed it into an existing monitoring system.

## SQL backup and restore

```bash
cd /opt/tg-auto-ad
runuser -u adbot -- .venv/bin/python scripts/backup.py
```

The tool creates `backups/auto_ad-<UTC timestamp>.dump` using a consistent `pg_dump` snapshot in custom format and verifies the catalogue with `pg_restore --list`. The backup covers permanent content, task state, message/audit logs and configuration; media is stored as Telegram `file_id`, not as local video or image originals. Backup files are mode 600 and are never rotated or deleted automatically. Copy them periodically to a controlled off-host location, and keep the private `.env` and service definitions separately. No scheduled backup job is configured today.

Restore into a **brand-new empty database** first; the restore tool refuses to overwrite the database currently referenced by `DATABASE_URL` and any database that already has tables:

```bash
runuser -u postgres -- createdb --owner=adbot auto_ad_recovery
runuser -u adbot -- .venv/bin/python scripts/restore.py backups/chosen.dump --target auto_ad_recovery
runuser -u adbot -- psql -d auto_ad_recovery -c 'SELECT count(*) FROM schema_migrations;'
```

After verifying the contents, stop both services, back up the current database, point the `DATABASE_URL` database name in `.env` at the recovered one, start the services and run the health check. When restoring an older backup, the state of messages already sent to Telegram cannot be rolled back: messages may exist that were sent but never recorded in the backup, so check the affected tasks and broadcasts to avoid duplicate sends. Rolling back the source also requires migrations to stay compatible; do not run destructive downgrade scripts.

## Troubleshooting Telegram connectivity

Troubleshooting on 2026-11-08 confirmed that the send connection can produce `ConnectTimeout` or a TLS `Hostname mismatch` while the service process is still alive. Use `systemctl status` to inspect both services, and judge together with the health check, the journal, task failure records and the actual API permissions — never treat a running process alone as proof that the business is healthy.

The private `.env` accepts an optional `TELEGRAM_API_IPS`: a comma-separated list of `api.telegram.org` addresses that the deployer has verified independently (the domain is operated by Telegram and its addresses change, so never copy an example). Confirm them first through independent DoH or authoritative resolution, and keep TLS verification against the normal hostname. This setting only affects exact hostname resolution inside this program's process; it does not change the system DNS/hosts, replace the API URL or disable certificate verification. If the addresses stop working, re-verify or clear this setting, then restart both the receiving service and the Worker.

`Chat not found` / `Member list is inaccessible` can also mean the bot was removed from the target. With "skip on failure" enabled a task keeps advancing and keeps the failure records; a scheduling batch marked `COMPLETED` does not mean every delivery succeeded — check the `SUCCESS` / `FAILED` delivery counts. Add the bot back to the target with admin and posting rights, re-bind it, then manually resume the paused tasks; do not replay every completed failing batch to the channel.

## BotFather and platform settings

1. Pick the bot under `/mybots`; the token lives only in the private `.env`.
2. The program registers `/start`, `/help` and `/cancel`, the admin-only `/admin`, the menu button and the bot description automatically.
3. This project needs no webhook domain or HTTPS endpoint; do not run another `getUpdates` or webhook consumer for the same token elsewhere.
4. The first binding of a target or source requires both the bot and the operator to be admins; a channel bot also needs posting rights. A group bot admin can receive source messages, and the privacy-mode setting is no substitute for admin authorization.
5. Custom-emoji eligibility follows Telegram's actual API. Every menu category icon was verified against the real icon set, and custom-emoji entities sent by an admin are preserved as-is with no manual ID entry. Display in channels and across clients still has to be tested scenario by scenario.

## Tests and capacity

```bash
cd /opt/tg-auto-ad
runuser -u adbot -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q
```

SQL tests connect only to `auto_ad_test` and create a random schema that is cleaned up afterwards; the Bot is a fake object and nothing is broadcast to users. `scripts/live_smoke.py` is a **real-send tool for admin private chats**: it sends menus and the colored-button / custom-emoji acceptance messages. `scripts/live_worker_smoke.py` runs in a random schema on the test database and really sends to one configured admin — a rotation with delete-old, and a single-recipient broadcast with progress — then cleans up; the target-admin check was replaced only for this private-chat diagnostic, so it does not prove channel/group permission acceptance. Neither script is safe to run repeatedly as an ordinary automated test.

Free sends share a global budget of about 20 per second, with about a 1.05-second gap per channel/private chat and about 3.1 seconds per group; a 429 triggers a global cooldown using Telegram's actual `retry_after`. For 10,000 targets a single round costs about 8 minutes 20 seconds on the global send floor alone, and deleting old messages, permission lookups, media, per-group limits and the network add more; a 1-minute minimum interval does not mean 10,000 targets can finish a round every minute. The next rotation round counts its interval from the end of the current batch and never backfills every missed round.

Long-term retention requires watching disk, database size, WAL, backup count and query time. Business content and logs are not cleaned up automatically today, and a full disk will affect service. Albums are a later extension; content is handled as a single text/photo/video/animation/document at a time. User-account sessions, arbitrary history traversal and bypassing protected content are not supported.

## Billing configuration and recovery

The billing database migration is `004_billing.sql`. An admin enters the OKPay merchant number and payment key inside the bot under **Admin → Billing → Payment config**; the payment key must never be stored in ordinary system configuration. The server `.env` must carry a dedicated `BILLING_ENCRYPTION_KEY`, which you can generate with the project virtualenv: `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. Generate it once, before the first configuration; never replace it casually once the service is live, or historical payment credentials can no longer be decrypted.

An SQL backup contains only the encrypted credentials, so a restore also needs the original encryption key and the private service configuration kept safely alongside it. When rotating the payment key, keep the old encrypted credentials for querying historical orders; do not delete them or export them into logs or source archives. In the inbox a plain admin private-chat message keeps only a redacted placeholder — the original text exists in memory only while it is processed, so an unfinished sensitive input has to be re-sent after a restart.

The Worker has its own payment-query loop and uses no external web callback or extra TCP listener. Pending orders are queried under an order lock, with at least a 30-second backoff and bounded concurrency; a create request that times out never rebuilds the order automatically. When the outcome is unclear the original order is kept, and recovering the link by calling the create endpoint again is forbidden.

For an original order whose link was lost and whose only problem was exact-link format validation, the official fixed payment link can be rebuilt once two signed queries both confirm the same unpaid order; other review states are not cleared automatically. Crediting a payment still requires an exact match on amount, currency, merchant and order, and the unique entitlement record and the order's `PAID` state commit in the same transaction. Recovering an unpaid link is not acceptance of a paid flow.

For the full rules on products, quota, expiry and admin operations see [Billing](billing.md); for the scope of this round of testing and launch see [Billing report](billing-report.md).
