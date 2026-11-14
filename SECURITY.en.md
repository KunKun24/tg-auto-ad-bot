# Security Policy

[中文](SECURITY.md) · **English**

## Reporting a vulnerability

If you find a security vulnerability, **do not open a public issue**. Contact us privately through either of the following:

- Telegram: [@sad1881](https://t.me/sad1881)
- GitHub Security Advisories: repository page → Security → Report a vulnerability

Please include reproduction steps, the impact and your environment (Python version, PostgreSQL version, whether this is a self-hosted deployment). We will fix confirmed issues as soon as possible and credit you in the release notes once the fix ships (unless you prefer to stay anonymous).

## What the repository never contains

This repository is the public edition; none of the following is **in the repository**, and please do not commit it either:

- Real `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`
- `BILLING_ENCRYPTION_KEY` (the encryption key for payment credentials)
- Production databases, `data/`, `logs/`, `backups/` and any real delivery receipt
- Server addresses, private deployment paths and operations credentials

The repository only ships the [.env.example](.env.example) placeholder. If you see real credentials in a commit, follow the procedure below immediately.

## Handling a credential leak

1. Run `/revoke` in BotFather to issue a new Token, and restart both services.
2. If `BILLING_ENCRYPTION_KEY` leaks: finish all outstanding historical orders first, then re-encrypt with a new key; **do not delete the old key**, or historical orders can never be decrypted and queried again.
3. If admin IDs or database credentials leak: change `ADMIN_IDS`, rotate the database role password, and check the audit log for anomalous operations.
4. Clean the history with `git filter-repo` or BFG; the leak is only considered resolved after a force push — deleting just the latest commit does nothing.

## Deployment recommendations

- Set `.env` permissions to `600` with the system account that runs the service as the owner; the database listens on localhost only.
- The bot opens no inbound port; do not add a separate web admin panel.
- Run `scripts/health.py` regularly, and keep the output of `scripts/backup.py` and the encryption key off-site.
- When rotating payment credentials, keep the old credential for historical order lookups; confirm there are no pending orders before disabling it.

## Payments

The project integrates a third-party USDT gateway (OKPay), **not Telegram's official Stars settlement**. Signatures, amounts, merchant and order binding are all verified, but the risk, compliance and settlement disputes of the third-party gateway itself are borne by the deployer.
