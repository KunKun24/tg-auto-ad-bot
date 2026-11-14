"""Real PostgreSQL UI smoke tests; offline Bot, no environment/token loading.

Run as adbot; every test migrates/drops a random schema in auto_ad_test only.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from telegram import Message

from adbot.db import Database
from adbot.ui import BotUI

TEST_DSN = "postgresql://adbot@/auto_ad_test?host=/var/run/postgresql"
OWNER, OTHER, ADMIN = 22001, 22002, 22003
CHAT_A, CHAT_B, SOURCE = -10030001, -10030002, -10040001


class MockBot:
    id = 999000

    def __init__(self):
        self.sent, self.edited, self.copied = [], [], []
        self.sequence = 100
        self.chats = {cid: NS(id=cid, type="channel", title=f"频道 {cid}", username=name)
                      for cid, name in ((CHAT_A, "targetone"), (CHAT_B, "targettwo"), (SOURCE, "sourceone"))}

    async def get_me(self):
        return NS(id=self.id)

    async def get_chat(self, ref):
        if isinstance(ref, str) and ref.startswith("@"):
            return next(c for c in self.chats.values() if c.username == ref[1:])
        return self.chats[int(ref)]

    async def get_chat_member(self, chat_id, user_id):
        return NS(status="administrator", can_post_messages=True,
                  to_dict=lambda: {"status": "administrator", "can_post_messages": True})

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        self.sequence += 1
        return NS(message_id=self.sequence)

    async def edit_message_text(self, text, **kwargs):
        self.edited.append((kwargs["chat_id"], text, kwargs))
        return NS(message_id=kwargs["message_id"])

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self.copied.append((chat_id, from_chat_id, message_id, kwargs))
        self.sequence += 1
        return NS(message_id=self.sequence)

    async def forward_message(self, chat_id, from_chat_id, message_id, **kwargs):
        return await self.copy_message(chat_id, from_chat_id, message_id, **kwargs)


@pytest_asyncio.fixture
async def rig():
    assert "/auto_ad_test?" in TEST_DSN
    control = await asyncpg.connect(TEST_DSN)
    schema = "ui_smoke_" + uuid4().hex
    db = Database(TEST_DSN)
    try:
        assert await control.fetchval("SELECT current_database()") == "auto_ad_test"
        assert await control.fetchval("SELECT current_user") == "adbot"
        await control.execute(f'CREATE SCHEMA "{schema}"')

        async def init(con):
            for typ in ("json", "jsonb"):
                await con.set_type_codec(typ, schema="pg_catalog", encoder=json.dumps, decoder=json.loads)

        db.pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=6,
                                            server_settings={"search_path": schema}, init=init, command_timeout=10)
        await db.migrate()
        bot = MockBot()
        settings = NS(admin_ids=(ADMIN,), workers=8)
        ui, peer = BotUI(db, bot, settings), BotUI(db, bot, settings)

        async def no_wait(*_args, **_kwargs):
            pass

        ui.engine.throttle = peer.engine.throttle = no_wait
        yield NS(db=db, bot=bot, ui=ui, peer=peer)
    finally:
        await db.close()
        assert schema.startswith("ui_smoke_") and len(schema) == 41
        await control.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.close()


def update(uid=OWNER, text=None, action=None, message=None):
    user = NS(id=uid, username=f"tester{uid}", first_name=f"User{uid}", last_name=None, language_code="zh")
    chat = NS(id=uid, type="private")
    query = NS(data=f"u:{uid}:{action}", answer=AsyncMock(), message=NS(message_id=50)) if action is not None else None
    if message is None and text is not None:
        message = Message.de_json({"message_id": 70, "date": int(datetime.now(timezone.utc).timestamp()),
                                  "chat": {"id": uid, "type": "private"}, "text": text}, None)
    return NS(my_chat_member=None, effective_user=user, effective_chat=chat, callback_query=query, message=message)


async def send(rig, text, uid=OWNER, peer=False):
    await (rig.peer if peer else rig.ui).handle_update(update(uid, text=text))


async def click(rig, action, uid=OWNER, peer=False):
    await (rig.peer if peer else rig.ui).handle_update(update(uid, action=action))


async def step(rig, action, uid=OWNER, peer=False):
    state = await rig.ui.state(uid)
    assert state is not None, f"missing session before {action}"
    await click(rig, f"w:{state['data']['nonce']}:{action}", uid, peer)


async def register(rig):
    for uid in (OWNER, OTHER, ADMIN):
        await send(rig, "/start", uid)
    assert await rig.db.fetchval("SELECT count(*) FROM users") == 3


async def bind_targets(rig):
    await click(rig, "new:chats")
    await send(rig, f"@targetone\n{CHAT_B}\n@targetone")
    assert await rig.db.fetchval("SELECT count(*) FROM user_chats WHERE owner_id=$1 AND active", OWNER) == 2


async def create_content(rig, title="SQL内容", body="正文", uid=OWNER):
    await click(rig, "new:contents", uid)
    await send(rig, title, uid)
    await send(rig, body, uid)
    assert (await rig.ui.state(uid))["state"] == "CONTENT_OPTIONS"
    await step(rig, "preview", uid, peer=True)
    assert (await rig.ui.state(uid))["state"] == "CONTENT_CONFIRM"
    await step(rig, "confirm", uid)
    assert await rig.ui.state(uid) is None
    return await rig.db.fetchval("SELECT id FROM contents WHERE owner_id=$1 ORDER BY id DESC LIMIT 1", uid)


async def create_task(rig, content_ids):
    await click(rig, "new:tasks")
    await send(rig, "多目标SQL任务")
    for cid in content_ids:
        await step(rig, f"toggle:{cid}")
    await step(rig, "next", peer=True)
    await step(rig, "all")
    await step(rig, "next", peer=True)
    await send(rig, "1:30")
    for flag in ("delete_previous", "skip_failed", "delete_failure_pause"):
        await step(rig, f"flag:{flag}")
    await step(rig, "source:0")
    await send(rig, "2")
    assert (await rig.ui.state(OWNER))["state"] == "TASK_CONFIRM"
    await step(rig, "confirm", peer=True)
    return await rig.db.fetchval("SELECT id FROM tasks WHERE owner_id=$1 ORDER BY id DESC LIMIT 1", OWNER)


@pytest.mark.asyncio
async def test_registered_users_binding_request_chat_and_source_link_without_index(rig):
    await register(rig)
    await bind_targets(rig)
    await click(rig, "new:sources")
    session = await rig.ui.state(OWNER)
    selector = rig.bot.sent[-1][2]["reply_markup"].to_dict()
    assert selector["keyboard"][0][0]["request_chat"]["chat_is_channel"] is True
    shared = NS(text=None, caption=None, chat_shared=NS(request_id=session["data"]["request_id"] + 1,
                                                       chat_id=SOURCE, title="untrusted title", username="forged"))
    await rig.peer.handle_update(update(message=shared))
    source = await rig.db.fetchrow("SELECT * FROM sources WHERE owner_id=$1", OWNER)
    assert source["title"] == rig.bot.chats[SOURCE].title
    assert source["username"] == "sourceone"
    assert await rig.db.fetchval("SELECT count(*) FROM source_messages") == 0
    await click(rig, "import")
    await send(rig, "https://t.me/sourceone/777?single")
    assert (await rig.ui.state(OWNER))["state"] == "SOURCE_MODE"
    await step(rig, "mode:copy", peer=True)
    await step(rig, "preview")
    assert rig.bot.copied[-1][:3] == (OWNER, SOURCE, 777)
    await step(rig, "confirm", peer=True)
    payload = await rig.db.fetchval("SELECT payload FROM content_versions")
    assert payload["type"] == "copy" and payload["source_message_id"] == 777
    await click(rig, f"index:{source['id']}:0")
    await click(rig, f"source:{source['id']}")


@pytest.mark.asyncio
async def test_content_native_entities_buttons_preview_and_concurrent_version_confirmation(rig):
    await register(rig)
    await click(rig, "new:contents")
    await send(rig, "版本测试")
    original = Message.de_json({"message_id": 70, "date": 1, "chat": {"id": OWNER, "type": "private"},
                               "text": "粗体 😀", "entities": [{"type": "bold", "offset": 0, "length": 2}]}, None)
    await rig.ui.handle_update(update(message=original))
    await step(rig, "buttons")
    await send(rig, "#p 官网 - https://example.com")
    await step(rig, "protect")
    await step(rig, "preview", peer=True)
    data = (await rig.ui.state(OWNER))["data"]
    assert data["payload"]["entities"] == [{"type": "bold", "offset": 0, "length": 2}]
    assert data["payload"]["protect_content"] is True
    assert data["payload"]["buttons"][0][0]["style"] == "primary"
    action = f"w:{data['nonce']}:confirm"
    await asyncio.gather(click(rig, action), click(rig, action, peer=True))
    assert await rig.db.fetchval("SELECT count(*) FROM contents") == 1
    assert await rig.db.fetchval("SELECT count(*) FROM content_versions") == 1
    cid = await rig.db.fetchval("SELECT id FROM contents")
    await click(rig, f"cedit:{cid}")
    await send(rig, "第二版正文")
    await step(rig, "preview")
    await step(rig, "confirm", peer=True)
    rows = await rig.db.fetch("SELECT version_no,payload FROM content_versions ORDER BY version_no")
    assert [r["version_no"] for r in rows] == [1, 2]
    assert rows[0]["payload"]["text"] == "粗体 😀"
    assert rows[1]["payload"]["text"] == "第二版正文"
    assert await rig.db.fetchval("SELECT current_version_id FROM contents") == await rig.db.fetchval("SELECT id FROM content_versions WHERE version_no=2")
    await click(rig, f"versions:{cid}:0")
    await click(rig, "list:contents:0")


@pytest.mark.asyncio
async def test_task_wizard_multitarget_flags_confirmation_and_actions(rig):
    await register(rig)
    await bind_targets(rig)
    first, second = await create_content(rig, "一"), await create_content(rig, "二")
    tid = await create_task(rig, [second, first])
    task = await rig.db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
    assert task["status"] == "DRAFT" and task["interval_seconds"] == 5400 and task["max_runs"] == 2
    assert task["delete_previous"] and task["skip_failed"] and task["delete_failure_pause"]
    assert [r["content_id"] for r in await rig.db.fetch("SELECT content_id FROM task_items WHERE task_id=$1 ORDER BY position", tid)] == [second, first]
    assert await rig.db.fetchval("SELECT count(*) FROM task_targets WHERE task_id=$1", tid) == 2
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events") == 0
    await click(rig, f"taskact:start:{tid}")
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "DRAFT"
    await step(rig, "confirm", peer=True)
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "RUNNING"
    await rig.ui.engine.schedule()
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries") == 2
    await click(rig, f"taskact:pause:{tid}")
    await step(rig, "confirm")
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "PAUSED"
    await click(rig, f"taskact:stop:{tid}")
    await step(rig, "confirm")
    await click(rig, f"tedit:{tid}")
    assert (await rig.ui.state(OWNER))["data"]["content_ids"] == [second, first]
    await click(rig, "home")
    await click(rig, "stats")
    await click(rig, "logs:0")


@pytest.mark.asyncio
async def test_admin_search_lists_config_ban_and_confirmed_broadcast_progress(rig):
    await register(rig)
    await bind_targets(rig)
    cid = await create_content(rig)
    tid = await create_task(rig, [cid])
    await rig.ui.engine.start_task(tid, OWNER)
    for action in ("admin", "list:users:0", "list:alltasks:0", "list:allcontents:0", "list:allchats:0",
                   f"list:tasks:0:{OWNER}", f"user:{OWNER}", "audit:0"):
        await click(rig, action, ADMIN)
    for term in (str(OWNER), f"@tester{OWNER}", "ACTIVE", "seen:2020-01-01:2099-01-01"):
        await click(rig, "search", ADMIN)
        await send(rig, term, ADMIN)
        assert any(str(OWNER) in b.text for row in rig.bot.sent[-1][2]["reply_markup"].inline_keyboard for b in row)
    previous_welcome = await rig.db.fetchval("SELECT value FROM app_settings WHERE key='welcome'")
    await click(rig, "configkey:welcome", ADMIN)
    await send(rig, "SQL欢迎消息", ADMIN)
    assert await rig.db.fetchval("SELECT value FROM app_settings WHERE key='welcome'") == previous_welcome
    await step(rig, "confirm", ADMIN, peer=True)
    assert await rig.db.fetchval("SELECT value FROM app_settings WHERE key='welcome'") == "SQL欢迎消息"
    await click(rig, f"ban:{OTHER}", ADMIN)
    await send(rig, "SQL封禁原因", ADMIN)
    await step(rig, "confirm", ADMIN)
    assert await rig.db.fetchval("SELECT status FROM users WHERE id=$1", OTHER) == "BANNED"
    before = len(rig.bot.sent)
    await send(rig, "/start", OTHER)
    assert len(rig.bot.sent) == before
    await click(rig, "new:broadcasts", ADMIN)
    await send(rig, "SQL广播正文", ADMIN)
    await step(rig, "preview", ADMIN, peer=True)
    assert await rig.db.fetchval("SELECT count(*) FROM broadcast_jobs") == 0
    data = (await rig.ui.state(ADMIN))["data"]
    action = f"w:{data['nonce']}:confirm"
    await asyncio.gather(click(rig, action, ADMIN), click(rig, action, ADMIN, peer=True))
    assert await rig.db.fetchval("SELECT count(*) FROM broadcast_jobs") == 1
    job = await rig.db.fetchrow("SELECT * FROM broadcast_jobs")
    assert job["progress_chat_id"] == ADMIN and job["progress_message_id"]
    assert {r["user_id"] for r in await rig.db.fetch("SELECT user_id FROM broadcast_recipients")} == {OWNER, ADMIN}
    await rig.ui.engine._broadcast_tick()
    await click(rig, f"broadcast:{job['id']}", ADMIN)
    stats = await rig.ui.engine.broadcast_stats(job["id"])
    assert stats["success"] == 2 and stats["pending"] == 0
    await click(rig, f"bact:pause:{job['id']}", ADMIN)
    await step(rig, "confirm", ADMIN)
    assert await rig.db.fetchval("SELECT status FROM broadcast_jobs") == "PAUSED"
    await click(rig, f"ban:{OTHER}", ADMIN)
    await step(rig, "confirm", ADMIN)
    assert await rig.db.fetchval("SELECT status FROM users WHERE id=$1", OTHER) == "ACTIVE"
    assert await rig.db.fetchval("SELECT count(*) FROM audit_logs WHERE actor_id=$1", ADMIN) > 10


@pytest.mark.asyncio
async def test_instant_source_wizard_uses_authorized_source_and_forward_mode(rig):
    await register(rig)
    await bind_targets(rig)
    cid = await create_content(rig)
    await click(rig, "new:sources")
    await send(rig, "@sourceone")
    sid = await rig.db.fetchval("SELECT id FROM sources WHERE owner_id=$1", OWNER)
    await click(rig, "new:tasks")
    await send(rig, "即时来源SQL任务")
    await step(rig, f"toggle:{cid}")
    await step(rig, "next")
    await step(rig, f"toggle:{CHAT_A}")
    await step(rig, "next")
    await send(rig, "1")
    await step(rig, "sources:0", peer=True)
    assert (await rig.ui.state(OWNER))["state"] == "TASK_SOURCES"
    await step(rig, f"source:{sid}")
    assert (await rig.ui.state(OWNER))["state"] == "TASK_MODE"
    await step(rig, "mode:forward", peer=True)
    await send(rig, "0")
    await step(rig, "confirm")
    task = await rig.db.fetchrow("SELECT * FROM tasks WHERE owner_id=$1", OWNER)
    assert task["instant_source_id"] == sid and task["source_mode"] == "forward"
    assert task["interval_seconds"] == 60 and task["max_runs"] == 0 and task["status"] == "DRAFT"
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events") == 0


@pytest.mark.asyncio
async def test_ban_pauses_running_tasks_clears_session_and_unban_does_not_resume(rig):
    await register(rig)
    await bind_targets(rig)
    cid = await create_content(rig)
    tid = await create_task(rig, [cid])
    await rig.ui.engine.start_task(tid, OWNER)
    await click(rig, "new:contents")
    assert await rig.ui.state(OWNER) is not None
    await click(rig, f"ban:{OWNER}", ADMIN)
    await send(rig, "暂停SQL用户所有任务", ADMIN)
    assert await rig.db.fetchval("SELECT status FROM users WHERE id=$1", OWNER) == "ACTIVE"
    await step(rig, "confirm", ADMIN, peer=True)
    assert await rig.ui.state(OWNER) is None
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "PAUSED"
    assert await rig.db.fetchval("SELECT status FROM users WHERE id=$1", OWNER) == "BANNED"
    await click(rig, f"ban:{OWNER}", ADMIN)
    await step(rig, "confirm", ADMIN)
    assert await rig.db.fetchval("SELECT status FROM users WHERE id=$1", OWNER) == "ACTIVE"
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "PAUSED"
    assert await rig.db.fetchval("SELECT count(*) FROM audit_logs WHERE action IN ('ban','unban')") == 2
