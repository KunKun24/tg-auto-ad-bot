from __future__ import annotations

import asyncio
import logging
import signal

from telegram import Bot
from telegram.request import HTTPXRequest

from .config import configure_logging, load_settings
from .db import Database
from .engine import Engine
from .payments import Payments


async def payment_checks(db, settings, stop):
    payments = Payments(db, settings)
    while not stop.is_set():
        try:
            await payments.tick()
        except Exception as exc:
            logging.getLogger(__name__).error('支付查单循环失败 error_type=%s', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass

log=logging.getLogger(__name__)


async def run():
    configure_logging()
    settings=load_settings()
    db=Database(settings.database_url)
    await db.open()
    await db.migrate()
    stop=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,stop.set)
    try:
        async with db.pool.acquire() as guard:
            if not await guard.fetchval('SELECT pg_try_advisory_lock(7150000003)'):
                raise RuntimeError('Worker已运行，不允许重复启动')
            request=HTTPXRequest(connection_pool_size=40,connect_timeout=10,read_timeout=30,write_timeout=30,pool_timeout=30)
            async with Bot(settings.token,request=request) as bot:
                engine=Engine(db,bot,settings)
                await engine.recover()
                log.info('Worker已启动 bot=@%s workers=%s',bot.username,settings.workers)
                payments_task = asyncio.create_task(payment_checks(db, settings, stop), name='payment-checks')
                failures=0
                try:
                    while not stop.is_set():
                        if payments_task.done():
                            await payments_task
                        try:
                            await engine.tick()
                            failures=0
                        except Exception:
                            failures+=1
                            log.exception('Worker循环失败 consecutive=%s',failures)
                            if failures>=5:
                                for admin_id in settings.admin_ids:
                                    await engine.notify(admin_id,'⚠️ 自动广告Worker连续异常，将退出并由systemd重启，请检查日志。')
                                raise
                        try:
                            await asyncio.wait_for(stop.wait(),timeout=0.5)
                        except TimeoutError:
                            pass
                finally:
                    stop.set()
                    await payments_task
                log.info('Worker安全停止，所有已返回的发送结果已保存')
    finally:
        await db.close()


if __name__=='__main__':
    asyncio.run(run())
