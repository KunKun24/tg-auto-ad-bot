# Automatic ad bot implementation contract

[中文](CONTRACT.md) · **English**

Design scale: about 2000 users / 10000+ targets. Python 3.12, python-telegram-bot 22.8, asyncpg, PostgreSQL 16. No web. The bot process persists `getUpdates`; the Worker runs separately. SQL permanently stores content/versions/raw updates/logs. Never put the Token in code, logs or tests.

This document is the interface contract between modules: it explains why the split exists and what each module must expose. It is not a tutorial; suggested reading order: start with the [README](README.en.md) and [Features](docs/en/features.md), then come back here.

## Module responsibilities

| Module | Files | Responsibility |
| --- | --- | --- |
| Configuration and infrastructure | `adbot/config.py`, `adbot/db.py`, `adbot/network.py` | Environment variable validation, connection pool, SQL migrations, log redaction, pinned API domain resolution |
| Main flow | `adbot/main.py`, `adbot/worker.py` | Update persistence and serial dispatch, startup self-checks, Worker scheduling loop, single-instance lock |
| Business engine | `adbot/engine.py` | Task state machine, idempotency, rate limiting, recovery, `UNKNOWN` handling, broadcasts |
| Interaction UI | `adbot/ui.py`, `adbot/billing_ui.py` | Private-chat menus, wizards, confirmations, admin console, billing management |
| Content | `adbot/content.py` | Content capture, entities, button parsing, send adapters, emoji fallback, input parsing |
| Entitlements and payments | `adbot/entitlements.py`, `adbot/payments.py` | Membership quota calculation, order state machine, signature verification, order lookup |

Cross-module calls may only use the public interfaces listed in this document; assess the blast radius before adding a dependency, and never edit another module's private implementation directly.

## Settings / DB
`Settings` dataclass fields: token:str, admin_ids:tuple[int,...], database_url:str, timezone:str='Asia/Shanghai', poll_timeout:int=25, workers:int=8. `settings = load_settings()` reads them from the project `.env`.
`Database(dsn)`; async `open()` / `close()`; property `pool:asyncpg.Pool`; async `execute(sql,*args)->str`, `fetch(sql,*args)->list[Record]`, `fetchrow(sql,*args)->Record|None`, `fetchval(sql,*args)`. Pass Python objects for JSON/JSONB (the codec is installed) and timezone-aware datetimes for TIMESTAMPTZ columns. `async with db.pool.acquire() as con: async with con.transaction(): ...`.
`async audit(db, actor_id:int, action:str, resource_type:str='', resource_id:str='', details:dict|None=None)`.

## content.py must provide
- `parse_interval(value:str)->int` (minutes or `H:MM`, minimum 60 seconds).
- `parse_chat_ref(value:str)->int|str` (numeric / @username / t.me/username; rejects invite links and message links as targets).
- `parse_source_link(value:str)->tuple[int|str,int]` (public/private t.me/c/ links; strips query parameters).
- `capture_message(message:telegram.Message)->dict`: the unified payload — type is text/photo/video/animation/document, plus text, entities (list of dicts), file_id, buttons (list of row dicts), protect_content:bool; preserves received custom_emoji entities and their UTF-16 offsets. Legitimate URL buttons/colors/icons are kept; unsafe callback buttons are not inherited.
- `parse_buttons(text:str, entities:list[dict]|None=None)->list[list[dict]]`: `#p`/`#r`/`#g`, `text - URL`, `&&` within a row, at most 10 per row and 100 in total; custom emoji received in button text are converted to icon_custom_emoji_id; supports explicit `{emoji:ID}` in text; line-number and URL errors are reported strictly.
- `build_markup(rows)->InlineKeyboardMarkup|None`
- `strip_custom_emoji(payload:dict)->dict` (removes only custom_emoji entities and button icon fields, keeping the original fallback text and other entities).
- `async deliver(bot:telegram.Bot,chat_id:int,payload:dict,*,allow_fallback:bool=True)->dict` returning `{'message_id':int,'degraded':bool,'link':str|None}`. When type=forward/copy the payload carries source_chat_id/source_message_id; forward has no custom buttons; copy supports buttons. A send falls back and retries only when a Telegram BadRequest explicitly reports a custom_emoji eligibility error; Timeout/NetworkError must never be retried automatically (leave that to engine). Use native entities/caption_entities; HTML/Markdown may carry a parse_mode field but entities must not be passed at the same time. Custom emoji must never be lost. Colors use the native style.
- `async discover_icons(bot)->dict[str,str]`: verifies through the official public custom emoji sticker set or the known official example IDs via getCustomEmojiStickers, finding icons for menu/chats/content/tasks/stats/settings/help/admin/broadcast and the like; never fabricate them, and plain emoji are allowed on failure. The main process persists the result to app_settings(key='icons',value=dict) at startup.
- `menu_button(text,callback_data=None,url=None,style=None,icon_id=None)->InlineKeyboardButton`.

## UI contract
`class BotUI: __init__(self,db:Database,bot:telegram.Bot,settings:Settings)`; async `handle_update(update:telegram.Update)->None`. main owns update persistence/replay and per-user serialization; the Application Dispatcher is not used. UI handles message (private chat, including commands/session input), callback_query and my_chat_member. channel_post is received by engine; UI does not handle source triggers.
UI imports `from .engine import Engine`, instantiates `Engine(db,bot,settings)` and calls the public API below; do not reimplement scheduling rules locally.
`await engine.create_task(owner_id:int,name:str,content_ids:list[int],chat_ids:list[int],interval_seconds:int,delete_previous:bool=False,instant_source_id:int|None=None,source_mode:str='copy',max_runs:int=0,skip_failed:bool=False,delete_failure_pause:bool=False)->int` creates a DRAFT; content_ids are contents.id (an instant source may also carry rotation content; at least one is required); `start_task` follows.
`await engine.start_task(task_id:int,owner_id:int)->None` (DRAFT/STOPPED publishes immediately for the first time, PAUSED keeps the remaining wait, ERROR requires the unknown to be resolved first); async `pause_task`, `stop_task`, `delete_task` (soft delete, history retained), `run_now`, `resolve_unknown(task_id,owner_id,action:str)` (action=retry/skip), `remove_source(task_id,owner_id)`; UI owns the user-visible double confirmation and the audit when an admin acts on behalf of an owner.
`await engine.create_broadcast(admin_id:int,payload:dict)->int` (a snapshot of every started user who is not banned/blocked), `broadcast_action(job_id,admin_id,action)` (pause/resume/cancel), `broadcast_stats(job_id)->dict`; after creating, UI sends a progress message and updates broadcast_jobs.progress_chat_id/progress_message_id so the Worker can edit it dynamically.
`await engine.schedule_test_delete(owner_id,chat_id,message_id,delay=30)`.
`await engine.ingest_source(message:Message, update_id:int)` is handled by main for channel_post and messages in authorized source groups; messages are indexed permanently; only new messages trigger RUNNING tasks. Nothing accumulates while paused; scheduled and instant work share the same lock. A source equal to a target, or a cyclic source relationship, is forbidden.

## SQL schema (UI uses the exact fields)
All ids are BIGSERIAL (users.id/chat_id are Telegram BIGINT); timestamps default to now().
users(id PK,username,first_name,last_name,language_code,status TEXT default ACTIVE,blocked BOOL default false,timezone TEXT default Asia/Shanghai,first_seen TIMESTAMPTZ,last_seen TIMESTAMPTZ,ban_reason TEXT default '').
chats(chat_id PK,type TEXT,title TEXT,username TEXT,bot_status TEXT default UNKNOWN,permissions JSONB default {},updated_at TIMESTAMPTZ).
user_chats(id PK,owner_id FK users,chat_id FK chats,active BOOL default true,authorized_at TIMESTAMPTZ,UNIQUE(owner_id,chat_id)). Unbinding only sets active=false; existing task_targets are not deleted.
sources(id PK,owner_id FK users,chat_id BIGINT,title TEXT,username TEXT,active BOOL default true,authorized_at TIMESTAMPTZ,UNIQUE(owner_id,chat_id)). Unbinding sets active=false and only blocks new selections; it does not stop existing tasks — actually stopping one requires engine.remove_source.
source_messages(id PK,chat_id BIGINT,message_id BIGINT,payload JSONB,received_at TIMESTAMPTZ,UNIQUE(chat_id,message_id)). Queries must JOIN that owner's sources authorization; admins may see everything, with an audit record.
contents(id PK,owner_id FK users,title TEXT,current_version_id BIGINT nullable,deleted BOOL false,created_at,updated_at).
content_versions(id PK,content_id FK contents,version_no INT,payload JSONB,created_at,UNIQUE(content_id,version_no)). Editing content creates a new version; UI updates current_version_id in a transaction and old versions are never modified. copy/forward are references; an ordinary capture type=... is a snapshot. Applying a version to tasks happens by re-saving or by task editing, and each event pins its version.
sessions(owner_id PK,state TEXT,data JSONB,expires_at TIMESTAMPTZ,updated_at TIMESTAMPTZ). 15min idle timeout; cancel/back clears it. Session state/data may be defined by the UI.
tasks(id PK,owner_id,name,status TEXT default DRAFT,interval_seconds INT,next_run_at TIMESTAMPTZ,remaining_seconds INT default0,current_index INT default0,delete_previous BOOL false,instant_source_id BIGINT nullable FK sources,source_mode TEXT default copy,max_runs INT default0,completed_runs INT default0,skip_failed BOOL false,delete_failure_pause BOOL false,deleted BOOL false,last_error TEXT default '',created_at,updated_at).
task_items(task_id FK tasks,position INT,content_id FK contents,PRIMARY KEY(task_id,position)).
task_targets(id PK,task_id FK tasks,chat_id BIGINT,last_message_id BIGINT nullable,last_sent_at TIMESTAMPTZ,UNIQUE(task_id,chat_id)).
publication_events(id PK,task_id FK tasks,kind TEXT [SCHEDULED/INSTANT/MANUAL],dedupe_key TEXT UNIQUE,payload JSONB,status TEXT default PENDING,scheduled_at TIMESTAMPTZ default now(),started_at,finished_at).
deliveries(id PK,event_id FK publication_events,target_id FK task_targets,chat_id BIGINT,status TEXT default PENDING [PENDING/SENDING/RETRY/SUCCESS/FAILED/UNKNOWN/SKIPPED],attempts INT default0,next_attempt_at TIMESTAMPTZ default now(),message_id BIGINT nullable,error TEXT default '',degraded BOOL false,delete_status TEXT,delete_error TEXT,started_at,finished_at,UNIQUE(event_id,target_id)).
message_logs(id PK,owner_id,task_id nullable,event_id nullable,chat_id,message_id nullable,operation TEXT [SEND/DELETE/TEST],status TEXT [SUCCESS/FAILED/UNKNOWN],error TEXT default '',degraded BOOL false,payload_version_id BIGINT nullable,created_at).
broadcast_jobs(id PK,admin_id,payload JSONB,status TEXT default RUNNING [RUNNING/PAUSED/CANCELLED/COMPLETED/ERROR],progress_chat_id BIGINT nullable,progress_message_id BIGINT nullable,last_progress_at TIMESTAMPTZ,created_at,updated_at).
broadcast_recipients(id PK,job_id FK broadcast_jobs,user_id,status TEXT default PENDING [PENDING/SENDING/RETRY/SUCCESS/FAILED/UNKNOWN/SKIPPED],attempts INT default0,next_attempt_at TIMESTAMPTZ default now(),message_id BIGINT nullable,error TEXT default '',degraded BOOL false,started_at,finished_at,UNIQUE(job_id,user_id)).
audit_logs(id PK,actor_id BIGINT,action TEXT,resource_type TEXT default '',resource_id TEXT default '',details JSONB default {},created_at).
app_settings(key TEXT PK,value JSONB,updated_at).
updates(update_id BIGINT PK,payload JSONB,status TEXT default PENDING,attempts INT default0,error TEXT default '',received_at,processed_at).
deferred_deletions(id PK,owner_id,chat_id,message_id,run_at TIMESTAMPTZ,status TEXT default PENDING,error TEXT default '',UNIQUE(chat_id,message_id)).

## Behavioral contract
For bindings and sources, the first authorization runs get_chat + get_chat_member(user) admin rights + bot admin/channel post permission; after a user loses admin rights or unbinds, existing tasks continue (an explicit user requirement) and are not revoked automatically, but the UI must warn. The bot's current permissions are still verified before every real send. A ban pauses all of that user's tasks; an unban does not start them automatically. A private index is readable only by the owner who was once authorized, or by an admin. An admin viewing someone else's content is audited.
Minimum rotation interval is 60s; input 50=3000s/1:30=5400s. After a batch finishes with everything successful, the cursor advances and next_run_at=finish time+interval. On partial success only the failed items are retried; a cap of 5 attempts / a permanent error pauses it to ERROR; with skip_failed on, failures are skipped and the cursor advances. Unknown is never retried automatically. Pausing preserves the batch and can be resumed, while in-flight sends may still return; queued instant events are cancelled and no source trigger is accepted while paused. SENDING rows left by a crash become UNKNOWN and pause the task/broadcast. Source messages queue one by one with a cap of 1000; beyond that the task pauses with an alert and never drops silently. Prevent source=target/cross-task cycles. Delete old before posting new; a failed delete continues by default, or becomes ERROR when the pause option is on. Albums are allowed as a documented future extension; the current five media types are complete.
Admins: overview; user search and details / ban and unban with double confirmation / view all creatives, tasks, targets, logs and audit records; configuration (minimum interval / quota / test message / contact admin); broadcasts with full create-preview-confirm / progress bar / pause, resume, cancel / failed-recipient view and export / unknown handling. Regular users: targets — bulk binding, verification, test, unbinding; creatives — create/version, edit preview, delete, protect, formatting, buttons; sources — binding, link import, index browsing; task wizard for multiple creatives and targets / interval / delete old / instant source / limited runs / skip failures / pause on delete failure / preview and confirmation; task start, pause, resume, stop, delete, manual publish and unknown handling; statistics — send/delete results / failure records / export; settings; tutorial.
