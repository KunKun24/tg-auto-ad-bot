"""Offline inbox redaction and ephemeral dispatch regressions; no DB/network/env."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from adbot import main
from adbot.billing_ui import SECRET_REDACTED
from adbot.ui import BotUI

ADMIN, OTHER = 71001, 71002
SECRET = 'unit-test-payment-secret-must-never-persist-123456'
SETTINGS = SimpleNamespace(admin_ids=(ADMIN,), workers=2)


def update(text=SECRET, *, uid=ADMIN, chat_type='private', caption=False, edited=False, update_id=9001, extra=False):
    message = {
        'message_id': update_id,
        'date': 1,
        'chat': {'id': uid if chat_type == 'private' else -10071001, 'type': chat_type},
        'from': {'id': uid, 'is_bot': False, 'first_name': 'Offline'},
    }
    message['caption' if caption else 'text'] = text
    if caption:
        message['photo'] = [{'file_id': 'offline', 'file_unique_id': 'offline', 'width': 1, 'height': 1}]
    if extra:
        message['reply_to_message'] = {
            'message_id': 1, 'date': 1, 'chat': message['chat'], 'text': SECRET,
            'entities': [{'type': 'text_link', 'offset': 0, 'length': 1, 'url': 'https://example.invalid/' + SECRET}],
        }
        message['quote'] = {'text': SECRET, 'position': 0, 'entities': [{'type': 'text_link', 'offset': 0, 'length': 1, 'url': 'https://example.invalid/' + SECRET}]}
        message['entities'] = [{'type': 'text_link', 'offset': 0, 'length': 1, 'url': 'https://example.invalid/' + SECRET}]
        message['caption_entities'] = [{'type': 'text_link', 'offset': 0, 'length': 1, 'url': 'https://example.invalid/' + SECRET}]
        message['external_reply'] = {'origin': {'type': 'hidden_user', 'date': 1, 'sender_user_name': SECRET}}
    return Update.de_json({'update_id': update_id, 'edited_message' if edited else 'message': message}, None)


class FakeDB:
    def __init__(self, state=None, rows=(), stop=None):
        self.state = state
        self.rows = list(rows)
        self.stop = stop
        self.calls = []
        self.errors = []
        self.claim = True

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if 'SELECT state FROM sessions' in sql:
            assert 'expires_at' not in sql
            return self.state
        if "status='PROCESSING'" in sql:
            if not self.claim:
                if self.stop:
                    self.stop.set()
                return None
            return args[0]
        raise AssertionError('Unexpected fetchval SQL: ' + sql)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if 'SELECT status,ban_reason FROM users' in sql:
            return {'status': 'ACTIVE', 'ban_reason': ''}
        raise AssertionError('Unexpected fetchrow SQL: ' + sql)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        assert "FROM updates WHERE status='PENDING'" in sql
        rows, self.rows = self.rows, []
        if not rows and self.stop:
            self.stop.set()
        return rows

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if "status='ERROR'" in sql:
            self.errors.append(args[1])
        if ("status='ERROR'" in sql or "status='DONE'" in sql) and self.stop:
            self.stop.set()
        return 'UPDATE 1'


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def delete_message(self, *args, **kwargs):
        self.deleted.append((args, kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [SECRET, 'normal admin draft', '/start', '/cancel', '/admin', '/help'])
@pytest.mark.parametrize('caption', [False, True])
async def test_all_private_admin_text_is_ephemeral_even_before_secret_session(text, caption):
    db = FakeDB()
    original = update(text, caption=caption)
    payload, transient, force = await main.durable_update_payload(db, original, SETTINGS)
    assert transient and not force
    assert payload['_billing_ephemeral'] and not payload['_billing_force_secret']
    assert payload['message']['text'] == SECRET_REDACTED
    assert (original.message.caption if caption else original.message.text) == text
    assert text not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,chat_type', [(OTHER, 'private'), (OTHER, 'group'), (ADMIN, 'supergroup')])
@pytest.mark.parametrize('command', ['/paytoken', '/paytoken@offline_bot', '/PAYTOKEN@Offline_Bot'])
@pytest.mark.parametrize('caption,edited', [(False, False), (True, False), (False, True), (True, True)])
async def test_explicit_paytoken_is_redacted_for_every_sender_chat_and_message_kind(uid, chat_type, command, caption, edited):
    payload, transient, force = await main.durable_update_payload(
        FakeDB(), update(command + ' ' + SECRET, uid=uid, chat_type=chat_type, caption=caption, edited=edited), SETTINGS)
    assert transient and force
    assert SECRET not in json.dumps(payload)
    assert payload['edited_message' if edited else 'message']['text'] == SECRET_REDACTED
    assert payload['_billing_force_secret']


@pytest.mark.asyncio
@pytest.mark.parametrize('caption,edited', [(False, False), (True, False), (False, True), (True, True)])
async def test_rebuild_removes_reply_quote_entity_and_external_reply_secret_copies(caption, edited):
    original = update(caption=caption, edited=edited, extra=True)
    assert SECRET in original.to_json()
    payload, transient, _force = await main.durable_update_payload(FakeDB(), original, SETTINGS)
    assert transient and SECRET not in json.dumps(payload)
    message = payload['edited_message' if edited else 'message']
    assert set(message) == {'message_id', 'date', 'chat', 'from', 'text'}
    assert message['text'] == SECRET_REDACTED


@pytest.mark.asyncio
@pytest.mark.parametrize('chat_type', ['private', 'group', 'supergroup'])
async def test_ordinary_user_message_not_rewritten_and_never_looks_up_admin_session(chat_type):
    db = FakeDB(state='BILLING_CONFIG_SECRET')
    original = update('ordinary draft', uid=OTHER, chat_type=chat_type, extra=True)
    payload, transient, force = await main.durable_update_payload(db, original, SETTINGS)
    assert payload == json.loads(original.to_json()) and not transient and not force
    assert not db.calls


@pytest.mark.asyncio
async def test_expired_secret_session_still_forces_secret_and_exit_commands_remain_normal_ephemeral():
    db = FakeDB(state='BILLING_CONFIG_SECRET')
    payload, transient, force = await main.durable_update_payload(db, update(), SETTINGS)
    assert transient and force and SECRET not in json.dumps(payload)
    assert all('expires_at' not in sql for sql, _args in db.calls)
    _payload, transient, force = await main.durable_update_payload(db, update('/cancel'), SETTINGS)
    assert transient and not force


async def dispatch(monkeypatch, original, *, use_memory=True, force=False, handler=None, db=None):
    payload, transient, _ = await main.durable_update_payload(FakeDB(), original, SETTINGS)
    stop = asyncio.Event()
    db = db or FakeDB()
    db.stop = stop
    db.rows = [{'update_id': original.update_id, 'payload': payload}]
    ephemeral = {original.update_id: (original, force)} if use_memory and transient else {}
    calls, sources = [], []

    class UI:
        def __init__(self, *_args):
            pass

        async def handle_update(self, received, *, billing_secret=False):
            calls.append((received, billing_secret))
            if handler:
                await handler(received, billing_secret)

    class Engine:
        def __init__(self, *_args):
            pass

        async def ingest_source(self, message, update_id):
            sources.append((message, update_id))

    monkeypatch.setattr(main, 'BotUI', UI)
    monkeypatch.setattr(main, 'Engine', Engine)
    bot = FakeBot()
    await asyncio.wait_for(main.process_inbox(db, bot, SETTINGS, stop, ephemeral), timeout=3)
    return SimpleNamespace(calls=calls, sources=sources, db=db, bot=bot, ephemeral=ephemeral)


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True])
async def test_memory_dispatch_uses_original_update_and_explicit_force_bit(monkeypatch, force):
    original = update()
    result = await dispatch(monkeypatch, original, force=force)
    assert result.calls == [(original, force)]
    assert not result.sources and not result.ephemeral
    assert SECRET not in repr(result.db.calls)


@pytest.mark.asyncio
async def test_recovered_placeholder_forces_secret_dispatch_even_original_force_was_false(monkeypatch):
    result = await dispatch(monkeypatch, update(), use_memory=False)
    received, force = result.calls[0]
    assert force and received.message.text == SECRET_REDACTED
    assert SECRET not in received.to_json() and not result.ephemeral


@pytest.mark.asyncio
@pytest.mark.parametrize('use_memory', [False, True])
async def test_group_explicit_token_uses_secret_ui_not_source_ingest(monkeypatch, use_memory):
    result = await dispatch(monkeypatch, update('/paytoken ' + SECRET, uid=OTHER, chat_type='group'), force=True, use_memory=use_memory)
    assert len(result.calls) == 1 and result.calls[0][1] and not result.sources
    assert not result.ephemeral


@pytest.mark.asyncio
async def test_ordinary_group_text_keeps_source_ingest(monkeypatch):
    result = await dispatch(monkeypatch, update('normal source', uid=OTHER, chat_type='group'))
    assert not result.calls and len(result.sources) == 1 and not result.ephemeral


@pytest.mark.asyncio
@pytest.mark.parametrize('exception_type', [RuntimeError, ValueError])
async def test_ephemeral_errors_never_log_secret_traceback_or_store_exception_text(monkeypatch, caplog, exception_type):
    async def fail(_update, _force):
        raise exception_type('SQL validation failed for ' + SECRET)

    caplog.set_level(logging.ERROR, logger='adbot.main')
    result = await dispatch(monkeypatch, update(), handler=fail)
    assert result.db.errors == [exception_type.__name__]
    assert SECRET not in repr(result.db.calls) and SECRET not in repr(result.bot.sent)
    assert SECRET not in caplog.text and not result.ephemeral
    records = [record for record in caplog.records if record.name == 'adbot.main']
    assert len(records) == 1 and records[0].exc_info is None


@pytest.mark.asyncio
async def test_recovered_ephemeral_error_is_also_scrubbed(monkeypatch, caplog):
    async def fail(_update, _force):
        raise RuntimeError(SECRET)

    caplog.set_level(logging.ERROR, logger='adbot.main')
    result = await dispatch(monkeypatch, update(), use_memory=False, handler=fail)
    assert result.db.errors == ['RuntimeError'] and SECRET not in caplog.text


@pytest.mark.asyncio
async def test_duplicate_claim_removes_ephemeral_without_dispatch(monkeypatch):
    db = FakeDB()
    db.claim = False
    result = await dispatch(monkeypatch, update(), db=db)
    assert not result.calls and not result.ephemeral


@pytest.mark.asyncio
async def test_missing_original_never_becomes_draft_or_payment_credential(monkeypatch):
    payload, _transient, _force = await main.durable_update_payload(FakeDB(), update(), SETTINGS)
    stop = asyncio.Event()
    db = FakeDB(rows=[{'update_id': 9001, 'payload': payload}], stop=stop)
    bot = FakeBot()
    screens = []

    async def state(_self, _uid):
        return {'state': 'CONTENT_BODY', 'data': {}}

    async def screen(_self, _uid, text, *_args, **_kwargs):
        screens.append(text)

    monkeypatch.setattr(BotUI, 'state', state)
    monkeypatch.setattr(BotUI, 'screen', screen)
    content_capture = AsyncMock(side_effect=AssertionError('placeholder became content'))
    monkeypatch.setattr(BotUI, 'content_options', content_capture)
    await asyncio.wait_for(main.process_inbox(db, bot, SETTINGS, stop), timeout=3)
    assert screens and not content_capture.called
    assert not db.errors and SECRET not in repr(db.calls)
    assert any('脱敏' in text for text in screens)


@pytest.mark.asyncio
async def test_state_transition_after_durable_redaction_dispatches_original_to_current_secret_session(monkeypatch):
    original = update()
    # At persistence time the callback has not yet entered the secret state.
    payload, transient, force = await main.durable_update_payload(FakeDB(), original, SETTINGS)
    assert transient and not force and SECRET not in json.dumps(payload)
    stop = asyncio.Event()
    db = FakeDB(rows=[{'update_id': original.update_id, 'payload': payload}], stop=stop)
    bot = FakeBot()
    observed = []

    async def state(_self, _uid):
        return {'state': 'BILLING_CONFIG_SECRET', 'data': {'nonce': 'offline', 'merchant_id': 'offline'}}

    async def billing_message(_self, uid, message, session, *, force_secret=False):
        observed.append((uid, message.text, session['state'], force_secret))
        return True

    monkeypatch.setattr(BotUI, 'state', state)
    monkeypatch.setattr(BotUI, 'billing_message', billing_message)
    ephemeral = {original.update_id: (original, False)}
    await asyncio.wait_for(main.process_inbox(db, bot, SETTINGS, stop, ephemeral), timeout=3)
    assert observed == [(ADMIN, SECRET, 'BILLING_CONFIG_SECRET', False)]
    assert not ephemeral and SECRET not in repr(db.calls) and not db.errors


@pytest.mark.asyncio
async def test_cancellation_cleans_running_and_actor_lock_waiting_ephemeral(monkeypatch):
    originals = [update(update_id=9001), update(update_id=9002)]
    rows = []
    for original in originals:
        payload, _transient, _force = await main.durable_update_payload(FakeDB(), original, SETTINGS)
        rows.append({'update_id': original.update_id, 'payload': payload})
    entered = asyncio.Event()
    never = asyncio.Event()

    class UI:
        def __init__(self, *_args):
            pass

        async def handle_update(self, _update, *, billing_secret=False):
            entered.set()
            await never.wait()

    monkeypatch.setattr(main, 'BotUI', UI)
    stop = asyncio.Event()
    db = FakeDB(rows=rows, stop=stop)
    ephemeral = {original.update_id: (original, False) for original in originals}
    consumer = asyncio.create_task(main.process_inbox(db, FakeBot(), SETTINGS, stop, ephemeral))
    await asyncio.wait_for(entered.wait(), timeout=3)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert not ephemeral


@pytest.mark.asyncio
async def test_failed_claim_cleans_ephemeral_before_bubbling_database_error(monkeypatch):
    original = update()
    payload, _transient, _force = await main.durable_update_payload(FakeDB(), original, SETTINGS)

    class ClaimFailureDB(FakeDB):
        async def fetchval(self, sql, *args):
            if "status='PROCESSING'" in sql:
                raise RuntimeError('offline claim unavailable')
            return await super().fetchval(sql, *args)

    stop = asyncio.Event()
    db = ClaimFailureDB(rows=[{'update_id': original.update_id, 'payload': payload}], stop=stop)
    ephemeral = {original.update_id: (original, False)}
    with pytest.raises(RuntimeError, match='offline claim unavailable'):
        await main.process_inbox(db, FakeBot(), SETTINGS, stop, ephemeral)
    assert not ephemeral
