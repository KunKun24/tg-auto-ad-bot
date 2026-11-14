#!/usr/bin/env python3
"""Explicit admin-only billing UI check; no orders, credentials, or broadcasts."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram import Bot
from telegram.request import HTTPXRequest

from adbot.config import load_settings
from adbot.db import Database
from adbot.ui import BotUI


class ProbeBot(Bot):
    async def send_message(self, *args, **kwargs):
        message = await super().send_message(*args, **kwargs)
        rows = message.reply_markup.inline_keyboard if message.reply_markup else []
        result = {
            'admin_id': message.chat_id,
            'message_id': message.message_id,
            'row_sizes': [len(row) for row in rows],
            'icons': sum(bool(button.icon_custom_emoji_id) for row in rows for button in row),
            'styles': sorted({button.style for row in rows for button in row if button.style}),
        }
        self.bot_data['messages'].append(result)
        return message

    @property
    def bot_data(self):
        return self._probe_data


async def main():
    settings = load_settings()
    db = Database(settings.database_url)
    await db.open()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'live_payment_tested': False, 'orders_created': False,
              'customer_broadcast': False, 'recipients': []}
    try:
        request = HTTPXRequest(connect_timeout=10, read_timeout=20)
        async with ProbeBot(settings.token, request=request) as bot:
            # Bot forbids arbitrary attributes; a private diagnostic field is allowed.
            object.__setattr__(bot, '_probe_data', {'messages': []})
            ui = BotUI(db, bot, settings)
            before = await db.fetchval('SELECT count(*) FROM billing_orders')
            report['bot'] = bot.username
            for uid in settings.admin_ids:
                if await db.fetchval('SELECT 1 FROM sessions WHERE owner_id=$1', uid):
                    report['recipients'].append({'admin_id': uid, 'skipped': 'existing session'})
                    continue
                await ui.home(uid)
                home = bot.bot_data['messages'][-1]
                assert max(home['row_sizes'], default=0) <= 2
                snap = await ui.entitlements.snapshot(uid)
                assert snap['exempt'] and snap['active'] and snap['channel_limit'] is None
                await ui.billing_membership(uid)
                await ui.billing_admin_callback(uid, ['home'], None)
                report['recipients'].append({'admin_id': uid, 'home': home,
                                             'membership_exempt': True,
                                             'payment_configured': (await ui.payments.config_summary(uid))['configured']})
            assert await db.fetchval('SELECT count(*) FROM billing_orders') == before
            report['messages'] = bot.bot_data['messages']
            report['billing_enabled'] = await db.fetchval("SELECT value FROM app_settings WHERE key='billing_enabled'")
            target = ROOT / 'data' / 'billing-live-check-20261108.json'
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            target.chmod(0o600)
            print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
