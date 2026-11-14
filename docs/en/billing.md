# Membership and Billing

[中文](../收费使用说明.md) · **English**

This guide supplements the existing usage guide. All purchase, order lookup and admin billing operations happen in the bot's private chat; there is no web dashboard and no payment-callback listener.

## Plans and quota

| Plan | Price | Validity | Publish target quota |
| --- | --- | --- | --- |
| Weekly | 10 USDT | 7 days | 20 + any permanent add-on quota you already hold |
| Monthly | 30 USDT | 30 days | Unlimited commercial targets |
| Quarterly | 66 USDT | 90 days | Unlimited commercial targets |
| Permanent add-on, 10 targets | 3 USDT | Permanent, adds target quota only | +10 |
| Permanent add-on, 50 targets | 10 USDT | Permanent, adds target quota only | +50 |

Add-ons are available only to a currently valid standard weekly member. Add-on quota is kept permanently and stacks; it is not reset when membership expires, but it does not replace an active membership period. Monthly and quarterly plans have unlimited targets meanwhile; permanent add-on quota is retained and still counts if you later switch to a weekly plan.

A “target” means a channel, group or supergroup. Usage is the deduplicated union of active target bindings and the active targets of every undeleted task: one target counts once even across several tasks, and sources do not count. Unbinding alone does not remove a target that is still in a task; you must unbind it and remove it from the task, or delete that task. Targets of paused or stopped tasks still count while the task is not deleted.

Unlimited commercial quota does not lift Telegram API rate limits, the both-side admin requirement, safety quotas, queue capacity or permission checks. Actual sending still obeys the existing system safety settings.

## Membership centre and renewal

“Membership” on the main menu shows the current plan, its start and expiry time, the end time of queued subscriptions, permanent add-on quota and used / limit. Times follow the UTC stated in the interface.

Subscriptions queue as segments in the order they are confirmed and credited. A new segment starts at the later of the current time when it is credited and the end of the last segment already purchased, adds the full purchased number of days, and neither overlaps nor truncates an existing plan. Buying a different plan does not change the current plan's entitlement immediately; the start time on the confirmation page is only an estimate at that moment, and the actual credit queue decides.

For example, with a weekly plan running and a monthly plan bought behind it, the remaining weekly time still allows 20 base targets and the monthly plan only unlocks unlimited targets when its segment starts. When a monthly or quarterly plan ends and a weekly plan takes over, if current usage exceeds the weekly total, all publishing is blocked rather than an arbitrary subset of targets being kept. Clean up the extra bindings and task targets first, then resume manually.

Membership expiry or exceeding the quota pauses publishing but keeps existing resources, history, cursors and the remaining paused wait. Buying, renewing, topping up quota or turning billing off never resumes tasks automatically; check the task details, then click start and confirm. Instant-source messages that arrived while membership was invalid are not backfilled after renewal.

Without a valid membership you can still view and tidy your data, save content drafts, unbind, remove task targets, stop or delete tasks, export logs, purchase and contact support. Adding target or source bindings, creating a task, sending a test to a target, start / resume, publish now and UNKNOWN resend must pass the membership and quota check of the actual resource owner. When an admin acts for a normal user, the admin's own permanent exemption does not bypass the user's gate.

## Purchase and orders

1. Open “Purchase / Renew” and choose a plan or an add-on that is currently available.
2. Read the amount, full duration, queue activation note and purchase terms, then click “Agree to terms and create order”. Nothing is created without that confirmation.
3. When the order is awaiting payment and has a link that passed the allow-list check, click “Pay via OKPay”. The payment link may only be the official OKPay Telegram entry; custom payment URLs are rejected.
4. Go back to “My orders”, select the order and click “Check payment result”. A check never creates a new order. The system also polls pending orders at a limited rate; entitlement is credited once, only after the signature, merchant, order, currency and exact amount have all been verified.
5. After crediting, go to the task and start it manually. Do not treat a screenshot of the payment success page as proof that the bot has credited you.

Repeated clicks on the same purchase confirmation use a fixed request key and map to the same local order. When the create request result is unclear the screen shows “create result pending verification” and never creates another payment request on its own; keep the original order and contact support to avoid a duplicate purchase or payment. Manually choosing a product again starts a new purchase flow — it is not a retry of the old order.

The order page shows the local order number, the merchant order number and the OKPay platform order number together. A normal order sits at “awaiting payment”; the green “Pay via OKPay” button opens the real payment bot entry, and after payment the system detects it and activates the plan automatically, with no admin confirmation. The check button only refreshes the result immediately.

Other states include creation in progress, automatic verification of the create result, paid / credited, automatic payment-entry recovery, payment data verification error and creation failed. Only a bad signature or a mismatch in the amount, order or another field stops crediting and asks you to contact support. A pending order is never deleted just because time passed; for an unclear result, keep the original order and contact support.

If an order went to review only because its payment link failed validation, a payment check restores the official payment link on the same order once two official signed queries both pass, both report unpaid and the platform order matches; the background also re-checks at a limited rate. This path never calls the create endpoint again and never grants entitlement without payment. Other manual-review reasons are not cleared automatically. The entry accepts the official account name in any casing and always normalizes to the official payment link; transfer, withdrawal, spoofed-domain or platform-order-mismatched links are rejected. Reopen the original order — do not create a new one just to get the button back.

Order entitlement always belongs to the bot user who created the order. Someone else may pay for it, but the paying account never replaces the local order owner. A normal user can only look up their own orders.

## Terms and payment risk

Normal payments are not refundable, except where law or platform rules require it, for duplicate charges and for correcting a service that was not delivered. If you need a correction, provide the local order number — never send a payment key.

Telegram digital services may fall under Stars policy. OKPay is a third-party payment channel; that does not mean Telegram endorses it or that it meets the requirements of every region or platform, and account limits, failed payments and service interruptions are possible. The purchase confirmation page repeats this warning; confirm that you accept it before buying.

Support: `@sad1881` by default; if an admin changed the contact, the bot's “Contact” page is authoritative.

## Admin billing management

Admin rights and the permanent membership / commercial quota exemption come only from the server-side `settings.admin_ids`. The current deployment admins are `100000001` and `100000002`; removing an ID from the configuration removes both the admin rights and the exemption, and a username never grants them. Admins are still subject to bans and send safety rules.

The entry is “Admin → Billing”. It shows the billing switch, the payment configuration status, a redacted key display, the merchant ID and order counts by state; you can look up a member and their orders by numeric user ID and adjust entitlement after a second confirmation.

### Payment configuration

1. Configure a valid `BILLING_ENCRYPTION_KEY` on the server first; the deployer generates and stores it safely. Never send this server-side encryption key through a content draft, ordinary system settings or chat — losing it makes historical payment credentials undecryptable.
2. Click “Payment configuration” and send the numeric OKPay merchant ID first; do not send the payment key at this step.
3. Read the key custody and rotation risks, then click to confirm and enter the dedicated key input state.
4. Send only the OKPay payment key, or send `/paytoken <key>`. The bot immediately makes a best effort to delete that Telegram message; a balance query with no fund movement runs only inside the dedicated flow and only while admin rights are still valid, and the new configuration is enabled after it succeeds. A failed verification keeps the old configuration, and the interface never echoes the key or the underlying error.
5. `/cancel` exits and keeps the old configuration. If the wizard times out, or after a restart only a redacted record remains or the bot asks you to resend, open the configuration flow again. Never mix a sensitive key into an ordinary body; `/paytoken` is intercepted with priority in any session and is not saved as content.

The dedicated key state stores only the merchant ID and a random nonce, never the plaintext payment key. Raw updates are redacted before they reach the database; the key travels only through process memory to configuration verification, and the database keeps the encrypted credential only. Telegram deletion is best effort and cannot guarantee that platform or client copies are gone.

Rotating the configuration keeps the old encrypted credential so historical unfinished orders can still be queried. If you disable the old key at OKPay, old orders may no longer verify; clear the historical unfinished orders first, then consider disabling it externally. Never delete the encrypted configuration or historical credentials to “tidy up orders”.

### Billing switch and entitlement adjustments

Enabling or disabling billing each needs a second confirmation. Enabling billing pauses every non-admin running task inside one transaction, without deleting resources, history or task cursors. Disabling billing does not start any paused task either. Do not treat the switch as a bulk resume button.

After looking a user up you can choose “Adjust entitlement”, enter “days extra-targets reason” — for example `7 10 outage compensation` — and confirm again. The reason is required and capped at 300 characters; one adjustment changes at most ±3650 days or 100000 permanent targets.

Positive days are appended as monthly segments with unlimited commercial targets; negative days are deducted from the tail of the newest queued segment. Permanent targets can be added or deducted, but never below 0. Each adjustment records the admin, the reason and the values, and never resumes tasks. Adjusting credited entitlement is not a refund: it never transfers funds, reverses a payment or deletes the original order.

Ban rules take precedence over membership and admin exemptions. A banned user cannot regain publishing through payment, add-on quota or an admin acting for them; lift the ban through the existing ban flow first, then start the task explicitly.

## Verification boundaries

The project's automated verification uses offline bot stand-ins, simulated payment responses and a separate test database; it performs no real payment, transfer or production broadcast. Until a real small-amount payment loop has been run, merchant credentials and real crediting should not be claimed as accepted.
