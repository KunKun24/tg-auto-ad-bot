# TG Auto Ad Bot Features

[中文](../功能介绍.md) · **English**

Bot: [@KunKunGuangGaoBot](https://t.me/KunKunGuangGaoBot). Send `/start` in a private chat to begin; the system admins are 100000001 and 100000002. All business and admin operations happen inside Telegram; there is no web dashboard.

## User features

| Feature | Current implementation |
| --- | --- |
| Target channels / groups | Bulk entry of usernames, links and chat IDs; a bottom picker for private channels and groups; both-side admin verification, re-check, test message deleted after 30 seconds, unbind |
| Creatives | Text, single photo, video, animation, document; preserves native Telegram formatting and custom-emoji entities; plain text / HTML / MarkdownV2; private-chat preview, then confirm |
| Content versions | Editing creates a new version; history preview; soft delete keeps references and logs; already-created publish events use an independent snapshot |
| Ad buttons | URL buttons; several buttons per row and line breaks; primary blue, success green, danger red; parses custom emoji straight from the message, or an explicit emoji ID |
| Source messages | Separate source authorization; shows the messages actually received; an authorized known message link is tried with the official copy / forward API; copy may carry buttons, native forward keeps source attribution |
| Scheduled rotation | Multi-content A→B→C loop across many targets; whole-minute or H:MM intervals; limited or unlimited repeats; a task is saved as a draft first, then confirmed and started |
| Instant mirroring | New source posts queue independently and are serialized with scheduled publishing; event deduplication and source-loop detection; instant events never advance the rotation cursor |
| Delete old, post new | Stores the last successful message per owner, task and target; only touches messages recorded by this task; continue or pause on delete failure is selectable |
| Task control | Start, pause, resume, stop, publish now, edit, remove instant source, delete; pause keeps the remaining wait; important actions need a second confirmation |
| Statistics and records | Today's stats in the user's timezone, task status, send / delete results, errors, usable public message links; batched CSV export |
| Personal settings | IANA timezone, tutorial, contact admin |

## System admin features

- Global users, active / new, bans, plus task and delivery queue statistics.
- Find users by ID, username, name, status and last-active date.
- View every user's targets, sources, creatives, tasks and logs; admin actions are written to an audit record.
- Ban reason and a second confirmation; a ban pauses running tasks and expires wizard sessions; unbanning does not restore them.
- Allow-listed system settings: minimum interval, task / target / binding quotas, instant queue, test copy, welcome text, tutorial and contact.
- Broadcasts support a body plus a single media item, formatting, custom emoji, colored / icon buttons and protected content. An admin can simply send a message carrying custom emoji — the program preserves the entities, with no emoji IDs to copy by hand.
- A broadcast is previewed in a private chat first and becomes an SQL task only after explicit confirmation; 20-cell live progress with success / failed / skipped / pending / unknown counts; refresh, pause, resume, cancel, export of failed / unknown recipients.

## Persistence and reliability

- PostgreSQL stores users, bindings, sources, content versions, tasks, sessions, publish snapshots, per-target deliveries, broadcast recipients, messages and audit records. Bodies and versions never expire automatically; media is kept as a Telegram file_id.
- Each user's data is isolated by owner, and delete pointers for shared channels are isolated per task; one physical chat shares its rate limit.
- The receiving process persists Telegram updates first, then processes them; the worker runs tasks independently. After a crash or reboot both recover from SQL.
- When a multi-target run partially succeeds, targets that already succeeded are not repeated automatically; explicit failures follow the policy, and retries honor Telegram retry_after.
- An uncertain network result is recorded as UNKNOWN and that chain is paused; the user checks manually, then explicitly resends or skips. Telegram and SQL cannot share one transaction, so exactly-once external delivery is not promised.
- The receiving process and the worker share the free-send budget across processes; paid Stars broadcasts are not enabled.
- SQL backup, empty-database restore and health-check tools are provided; both services start on boot and restart on failure.

## Authorization and platform boundaries

The first binding must verify that both the user and the bot are admins. Under the confirmed rules, later unbinding or a lost admin status **does not stop existing tasks**; the user must pause or stop them, or explicitly remove the source / target from the task. If the bot itself lacks permission, delivery still fails and the error is recorded.

Custom emoji and button icons depend on the send context, the account's Telegram eligibility and what the client actually renders; only an explicit custom-emoji entitlement rejection triggers the compatible downgrade, and other API errors or unknown network results are never dressed up as success. All nine menu icon types were verified against the real Telegram icon set.

This version handles a single media item; albums are a future extension. The Bot API cannot enumerate arbitrary history; the bot uses no user-account sessions, SMS codes, unofficial protocols or attempts to bypass protected content.

Free sending has global and per-chat limits: 10000 targets take at least 8 minutes 20 seconds even at only 20 messages per second, and usually longer; a one-minute minimum interval does not mean every target is reached within a minute.
