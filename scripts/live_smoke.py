#!/usr/bin/env python3
"""Explicit admin-only live menu/emoji smoke check; never broadcasts to users."""
from __future__ import annotations
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from telegram import Bot, MessageEntity, Update
from telegram.error import TelegramError
from adbot.config import load_settings, configure_logging
from adbot.content import discover_icons, deliver
from adbot.db import Database
from adbot.engine import Engine, safe_error
from adbot.ui import BotUI


class ProbeBot(Bot):
    async def send_message(self,*args,**kwargs):
        message=await super().send_message(*args,**kwargs)
        keyboard=message.reply_markup.inline_keyboard if message.reply_markup else []
        print(json.dumps({'kind':'send_message','chat_id':message.chat_id,'message_id':message.message_id,
                          'buttons':sum(len(row) for row in keyboard),
                          'button_icons':sum(bool(button.icon_custom_emoji_id) for row in keyboard for button in row),
                          'button_styles':[button.style for row in keyboard for button in row if button.style],
                          'custom_entities':sum(entity.type=='custom_emoji' for entity in message.entities or ())},ensure_ascii=False))
        return message


async def main():
    configure_logging()
    settings=load_settings()
    db=Database(settings.database_url)
    await db.open()
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'recipients':[]}
    try:
        async with ProbeBot(settings.token) as bot:
            icons=await discover_icons(bot)
            await db.execute("INSERT INTO app_settings(key,value) VALUES('icons',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",icons)
            report.update(bot=bot.username,icons=icons)
            ui=BotUI(db,bot,settings)
            engine=Engine(db,bot,settings)
            for index,uid in enumerate(settings.admin_ids):
                item={'admin_id':uid}
                try:
                    chat=await bot.get_chat(uid)
                    payload={'update_id':9_000_000_000+index,'message':{'message_id':1,'date':int(datetime.now(timezone.utc).timestamp()),'chat':{'id':uid,'type':'private'},'from':{'id':uid,'is_bot':False,'first_name':chat.first_name or 'Admin','username':chat.username},'text':'/start'}}
                    await ui.handle_update(Update.de_json(payload,bot))
                    item['menu']='delivered'
                    if index==0:
                        await engine.throttle(uid,'private')
                        emoji=next(iter(icons.values()),'5368324170671202286')
                        result=await deliver(bot,uid,{'type':'text','text':'⭐ 真实API验收：彩色按钮、会员表情、菜单与SQL已接通。\n这条消息仅发送给管理员。','entities':[{'type':'custom_emoji','offset':0,'length':1,'custom_emoji_id':emoji}], 'buttons':[[{'text':'打开机器人','url':f'https://t.me/{bot.username}','style':'primary','icon_custom_emoji_id':emoji},{'text':'验收完成','url':f'https://t.me/{bot.username}','style':'success'},{'text':'使用教程','url':f'https://t.me/{bot.username}?start=help','style':'danger'}]]})
                        item['emoji_check']=result
                except TelegramError as exc:
                    item['error']=safe_error(exc)
                report['recipients'].append(item)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        (ROOT/'data'/'live-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        await db.close()


if __name__=='__main__':
    asyncio.run(main())
