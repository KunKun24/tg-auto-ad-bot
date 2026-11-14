# Billing Module: Notes and Acceptance

[中文](../收费升级报告.md) · **English**

This document explains why the membership/billing module is designed this way, how far acceptance went, and the real pitfall encountered along the way. For operating instructions see [Billing](billing.md); for the interface contract see the [billing module contract](../../BILLING_CONTRACT.md).

Demo bot: [@KunKunGuangGaoBot](https://t.me/KunKunGuangGaoBot).

## Delivery rules

- Weekly plan: 10 USDT / 7 days, 20 basic publishing targets.
- Monthly plan: 30 USDT / 30 days; quarterly plan: 66 USDT / 90 days. The commercial target limit does not apply while the plan is valid, but the safety quota and Telegram rate limits still do.
- Permanent add-ons: 10 targets for 3 USDT, 50 targets for 10 USDT; only a currently valid weekly membership can buy them. The quota accrues permanently, does not replace membership time and is not reset on expiry.
- Targets are the **de-duplicated union** of a user's valid bindings and the valid targets of tasks that have not been deleted, including channels, groups and supergroups; sources are excluded, and the same target counts once across tasks. Unbinding alone does not reduce usage — a target still referenced by a task keeps counting.
- Renewals **queue in segments** in the order payments are confirmed, never truncate time already bought and never charge automatically. Each plan takes effect when its own segment starts.
- Expiry, exceeding the quota, or billing being switched on all pause the affected tasks without deleting data. Buying, renewing, adding quota or switching billing off **never resumes tasks automatically**; the user inspects them and starts them manually. Instant source events that arrived while membership was inactive are not replayed.
- Admins (`settings.admin_ids`) are permanently exempt from payment and the commercial target quota; bans, permissions and safety rate limits still apply.
- All 13 buttons on the home menu carry a custom-emoji icon, at most two per row; Membership, Purchase, Orders and Billing use colored buttons with action hints.

## Payment and security design

An admin configures the OKPay merchant number and payment key inside the bot; there is no web dashboard and no callback port is opened:

1. The server `.env` provides a dedicated `BILLING_ENCRYPTION_KEY` (Fernet); payment credentials are encrypted with it before being written to the database.
2. The inbox replaces an admin private-chat message with a redacted placeholder **before persistence**; the key is handed to configuration validation in process memory only and never enters `sessions`, raw updates, audit bodies, exception messages or logs.
3. Validation runs a single balance query against the official signed API that moves no funds, and activates the new configuration only if it passes; on failure the old configuration is kept, and the UI echoes neither the key nor the underlying exception.
4. When rotating, the old encrypted credentials are kept so historical unfinished orders can still be queried; finish those orders before disabling the old key.
5. The payment protocol follows the [official documentation](https://docs.okaypay.me/doc.html): uppercase HMAC-SHA256 signatures, the fixed official API, HTTPS certificate verification and no automatic redirects. Order queries are made twice with signatures and check merchant, order binding, amount, currency and status.
6. The unique entitlement record and the order's `PAID` state commit in **the same transaction**, so a repeated query never grants twice.
7. When a create request has an unclear outcome, `payLink` is not called again and the original order is kept; the worker's independent query loop checks pending orders under an order lock with at least a 30-second backoff and bounded concurrency.

Deleting the user's key message on Telegram is **best effort**: copies held by the platform or the client are not guaranteed to be gone. The project also never stores the payment key in content drafts, ordinary system configuration or audit records.

## A real pitfall: why the pay button never appeared

After launch a user reported that a new order was created successfully but the message contained **no pay button**. The original order was patched once against the example in the official documentation, yet new orders still entered `REVIEW` — because that fix was validated against the documentation example rather than a real creation response, and never addressed the root cause.

A real, signature-valid `payLink` response actually returns:

```text
https://t.me/OkayPayBot?start=shop_deposit--{order_id}     ← real response
https://t.me/okpay?start=shop_deposit----{order_id}        ← official doc example
```

The two differ **in the case of the account name and in the number of hyphens** : the real response uses the capitalised `OkayPayBot` with two hyphens, while the documentation example uses lowercase `okpay` with four. The original local implementation validated strictly against the format in the doc example, so it rejected the real entry point — a new order ended up with no pay button; and even when a button was rendered, the wrong entry point cannot open the corresponding order.

The final implementation accepts both prescribed formats and standardises on the two-hyphen `OkayPayBot` entry point verified against the real response. Not one security constraint was relaxed:

- the link suffix must **exactly equal** the platform order id in the signed response;
- HTTPS, signature, merchant and amount checks stay in place;
- forged domains, embedded user information, ports other than 443, duplicate parameters, transfer/withdrawal purposes and bindings that do not match the order are all rejected.

For an original order that entered review only because of "exact link format validation", the correct entry point is restored only after **two official signed queries both confirm the same unpaid order**; other review reasons are never cleared automatically. A temporary network error, a warning or a status mismatch only preserves the right to retry later — it never grants an entitlement on that basis.

The UI also no longer renders every `REVIEW` state as "waiting for manual review": a link-only problem shows "payment entry recovering automatically", while a signature/amount/order mismatch shows "payment data validation error". After a normal payment the query loop activates the entitlement automatically, with no admin confirming the credit.

## Verification results

The full automated suite, run under the production service account:

```text
481 passed, 10 warnings in 134.50s
```

- **132 payment tests**: real signed response format, a new order being payable immediately, compatibility with the old documented entry point, strict URL validation, recovery of the same order, signature/merchant/order/amount/currency checks, conflicting double queries, concurrency and duplicate crediting, credential rotation, backoff and error handling.
- **59 billing UI tests**: the real payment entry point, display of the three order id types, automatic crediting and error hints, case compatibility, two-column menus, plan confirmation, permissions and the key-entry flow.
- Membership, inbox redaction, existing business logic, real isolated SQL, scheduling reliability and local capacity tests all passed as well.
- The 10 warnings are python-telegram-bot deprecation notices about a future `retry_after` type change; the implementation already accepts both a number and a `timedelta`.

On the real Telegram side it was verified that: each admin private-chat home menu returns 13 buttons and 13 icons (row widths 2/2/2/2/2/2/1); the membership center and billing admin menus sent successfully; the original order's pay button was verified through a real Telegram response; and after a user created an order from the real menu, the callback was processed as `DONE` with an entry suffix matching that order.

**Not yet accepted**: the complete paid loop from a real payment to the entitlement being granted. Automated tests use a separate test database and simulated payment responses; they never make real payments, transfer funds or broadcast to customers. **A working button is not payment acceptance** — walk through a real payment with the smallest amount yourself before launch.

## Data and migration

Migration `004_billing.sql` only adds tables and configuration entries and does not modify existing business tables, so existing users, content, versions, tasks and publish records have identical counts before and after it.

Before the upgrade the source and private configuration were stored in a private backup directory; an SQL backup was taken before the downtime migration, and **the encryption key is kept separately and securely** — an SQL backup without the key leaves historical payment credentials undecryptable. The two restore together but must not be distributed through a source archive or a public repository.

## Compliance note

[Telegram's digital-services payment rules](https://core.telegram.org/bots/payments-stars) require digital services to settle in Stars. This implementation connects to a third-party USDT channel and **does not imply Telegram approval or compliance with the regulatory requirements of any jurisdiction**; account limits, failed payments and service interruption are possible, and the purchase confirmation page keeps the risk and terms notice. Whether to adopt it is the deployer's own decision and responsibility.
