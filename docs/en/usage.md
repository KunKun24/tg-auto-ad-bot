# TG Auto Ad Bot Usage

[中文](../使用说明.md) · **English**

## Getting started and safety boundaries

Send `/start` or `/menu` to the bot in a private chat. All business happens inside the bot; there is no web dashboard. `/cancel` returns to the main menu and voids the current session, `/help` opens the tutorial, and system admins can use `/admin`.

Menus and operation records are kept permanently in PostgreSQL. A wizard session is stored for 15 minutes, and an unexpired step can continue after a process restart; after a timeout, a cancel, returning to the main menu or switching feature, start over. Menu callbacks are bound to the operator ID, so forwarding someone else's menu cannot operate on their resources.

Users can only access their own targets, sources, creatives, tasks and logs. A system admin's cross-user views and edits go into the audit log. Business requests from banned users are silently ignored and their running tasks are paused; unbanning does not restart tasks. Any client-side result is subject to the database log and the actual Telegram response.

## Binding target channels and groups

1. Make both yourself and the bot admins of the target. For a channel you must also grant the bot permission to post.
2. Open “Targets → Add”, then use the bottom channel / group picker to choose a private target, or enter one `@username`, `https://t.me/username` or numeric chat ID per line — 1 to 100 per batch, and you can submit several batches. The picker also requires both-side admin rights, and the returned chat ID is re-verified on the server; source binding uses the same picker.
3. The bot checks both-side permissions live and reports the successful channel and group counts plus each failure reason. A normal user, an invalid link, an invite link, a non-admin or a bot without posting permission will not bind.
4. Target details offer re-check, test message and unbind. A test really sends, and on success schedules deletion after 30 seconds — check the log to see whether the deletion worked. Before production acceptance, do not press test on unrelated targets.

The first binding verifies the user's and the bot's admin status together. After binding, a user leaving or losing admin rights, or unbinding a target from the menu, does not stop existing tasks automatically; unbinding only keeps the target out of future task selection. If the bot itself lacks permission it cannot send, and the task error is recorded. To actually stop publishing you must explicitly pause or stop the task, or pause it and edit the task to remove the target.

## Saving creatives

In “Creatives → Add” enter a title, then send text, a single photo, video, animation or document. The received body, caption, Telegram entities, custom emoji IDs and media file_id are saved as a version.

Format options: the native entities as received, plain text, HTML, MarkdownV2. After choosing HTML or MarkdownV2 the bot no longer sends entities at the same time, avoiding formatting conflicts; a syntax error must be fixed and previewed again, it is not treated as a successful save. Every time, the private-chat preview must succeed before you click to confirm the save.

“Edit new version” produces a new version and never rewrites the old one or the publish snapshot of events already created. “History” is a read-only preview. “Delete” needs a second confirmation and is a soft delete: it blocks new selection but keeps existing references and history; check the related tasks as well.

### Button syntax

```text
#p Official site - https://example.com
#g View campaign - https://example.com/a && #r Contact support - https://t.me/example
#p {emoji:5368324170671202286} View details - https://example.com
```

`#p` is blue, `#r` red, `#g` green. Separate buttons on one row with `&&`; a line break starts a new row; send a lone `-` to clear the buttons. You can send a custom emoji carrying a custom-emoji entity directly, or write `{emoji:numeric id}` explicitly. Button links must use the safe addresses the parser allows; an invalid line returns its line number and reason, and nothing is partially saved.

Whether custom emoji can be sent still depends on the account's current Telegram eligibility and permissions. Only an explicit emoji entitlement error triggers a fallback to the original text, and the downgraded state is recorded; an ambiguous network error is never passed off as emoji being unsupported, and no blind retry happens. The bot never fabricates emoji IDs. When menu icons have no valid configuration, explicit plain-emoji defaults are used; if Telegram explicitly rejects the custom-icon entitlement, the menu drops the icon IDs, keeps the color styling, retries at most once and writes an audit record. Menus, private-chat previews, tests and CSV delivery all go through the engine's rate limiter.

## Sources and history limits

Sources and targets are bound separately, and the first authorization also checks both-side admin rights. In “Sources” browse the index of messages the bot actually received and saved, pick one to import, or enter a source message link in the content list.

The Bot API cannot enumerate arbitrary history; the source index only shows messages the bot actually received. For a known message link, the bot first verifies that the source was once authorized by the current user, then uses the official copy / forward API to try a preview in the private chat; even if the bot never saved that index entry, it does not assert up front that copying is impossible. Access rights, protected content or a deleted message can still make it fail — the real API response decides, and the bot never substitutes a fake message or claims to have read one.

After importing, choose “Copy” or “Forward”. Copy keeps the body / media and may add buttons; forward keeps the source attribution and does not support custom buttons. Protected content or Telegram limits may make copy / forward fail; the preview result decides.

Unbinding a source only hides it from new selections; it does not automatically stop instant listening in existing tasks. To really stop an instant source, click “Confirm removal of instant source” in the task details, or stop / delete the task.

## Creating and managing tasks

In “Rotation tasks → Add” complete the wizard: name → several creatives → several targets → interval → delete-old and failure policy → optional instant source and copy / forward mode → maximum rounds → preview and confirm. Creatives are ordered by click order; select-all follows list-ID order. Both creatives and targets support paging, select-all and clear, and the target count is capped by quota.

The interval is a whole number of minutes or `H:MM` — `50` is 50 minutes, `1:30` is 90 minutes. The minimum is 60 seconds and an admin can raise it. A maximum rounds value of `0` means unlimited. Saving a task only creates a draft and never sends to targets automatically; you must click start in the details and confirm again.

- Start / resume: resuming from a pause keeps the remaining wait instead of recomputing the interval as a full round.
- Pause: stops scheduling further publishing and saves the remaining wait; requests already in flight can still return a result.
- Stop: stops the current task chain; history logs are not deleted.
- Publish now: creates an event through the scheduling engine and does not bypass database state or permission checks.
- Edit settings: only in draft, paused, stopped or completed state; pending events must be handled or stopped first. Adjusting targets keeps the historical records. Editing a completed task turns it into stopped with the completed rounds reset, and you must start it again explicitly.
- Delete task: second confirmation, then a soft delete; history, messages and audit records remain.
- Delete old before sending: the previous matching message is handled before this round goes out; a delete failure continues by default and is logged, or you can choose to pause on delete failure.
- Skip on failure: the engine handles explicit failures per the task policy and never treats an uncertain result as a failure and resends on its own.

New instant-source posts and scheduled tasks are scheduled, queued and snapshotted by the same engine. Source loops, illegal cross-user references, over-quota targets and invalid states are rejected. When the UI hits a Telegram `RetryAfter` it updates the cooldown shared by all instances and asks the user to retry later; it does not automatically resend the original business operation, and an unknown network result is not retried automatically either.

### Handling UNKNOWN

A network break or a process fault can leave a message already received by Telegram while the bot does not know the outcome; that is marked `UNKNOWN`. The database and Telegram share no transaction, so exactly-once external delivery cannot be guaranteed.

First check the actual message in the target manually, then choose “UNKNOWN resend” (a duplicate is possible) or “UNKNOWN skip” (a miss is possible); both need a second confirmation. Do not click publish now repeatedly instead of confirming. A service restart never resends an uncertain request unconditionally.

## Statistics, logs and personal settings

“Statistics” shows today's send / delete status in the user's current timezone, the task status breakdown, the binding count and recent errors. The message record pages through the operation, status, chat ID, error and any public message link that can be constructed; private messages or targets without a username never get a fabricated public link.

CSV export reads in batches and holds at most 10000 rows per file, covering send and delete results plus the content version. Cells escape formula prefixes so a spreadsheet application does not execute them on open. An export goes only to the requester; an admin can export a given user's data from the user details.

In “Settings” you save an IANA timezone such as `Asia/Shanghai` or `Europe/London`; it sets the boundary of the user's today statistics. Task scheduling is managed in database UTC time. Contact admin and the tutorial open from the main menu.

## System admin

The default system admin IDs are `100000001` and `100000002`, and a deployment can add more. The admin entry opens for admins only; a normal user forging a callback gains no admin rights.

The overview shows total users, new today, active in the last 24 hours, bans, running tasks, the pending / unknown queue, target type counts and the server's operations for the day. Users can be searched by ID, username, name or `ACTIVE`/`BANNED`; the last-active range syntax is `seen:YYYY-MM-DD:YYYY-MM-DD`, dates are read as UTC, at most 50 rows are shown, so narrow the query.

User details show the basic profile, first / latest activity, resource counts, the ban reason and the user's targets, sources, tasks, creatives and logs. A ban requires a reason and a confirmation, and also pauses running tasks and invalidates sessions; unbanning only lifts the ban and does not start anything. A system admin cannot be banned through this UI.

“All tasks”, “All creatives” and “All targets” allow cross-user views and edits, all audited. System settings accept allow-listed keys only: minimum interval, task / target / binding quotas, instant queue cap, test copy, welcome text, tutorial and contact; changes get type and range validation, a second confirmation and before / after audit values. The menu can never read or change the token or the database password.

### Broadcast

1. “Admin → Broadcast → New”, send the body / media and set the format, buttons and protection options.
2. It first goes only to the admin's private chat as a preview; previewing never broadcasts.
3. Only “Confirm broadcast” creates the real broadcast task, and the engine filters recipients by current eligibility. The confirmation session is consumed atomically in SQL, so repeated or concurrent confirmations never create the same session broadcast twice.
4. The details show a 20-cell progress view, completion rate, success / failed / skipped / pending / UNKNOWN and the latest failure reasons; refresh, pause, resume, cancel unsent items and a failed / unknown detail CSV are supported.
5. Cancelling does not withdraw messages already sent successfully, and in-flight sends can still complete. UNKNOWN needs a human decision to resend or skip; afterwards the broadcast sits paused — check it, then click resume.

A broadcast is a production operation. This project's automated tests use fake objects only: they never read the token, start the bot or broadcast to real users. For formal acceptance, first use a separate test bot, an isolated database and a few accounts you own, then have an admin explicitly confirm the production broadcast.
