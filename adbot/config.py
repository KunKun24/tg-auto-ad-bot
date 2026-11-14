from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .network import configure_telegram_dns

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: tuple[int, ...]
    database_url: str
    timezone: str = 'Asia/Shanghai'
    poll_timeout: int = 25
    workers: int = 8


def load_settings() -> Settings:
    load_dotenv(ROOT / '.env', override=False)
    token = os.environ.get('BOT_TOKEN', '')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise ValueError('BOT_TOKEN 未配置或格式不正确')
    admins = tuple(int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip())
    if not admins:
        raise ValueError('至少需要一个管理员')
    configure_telegram_dns()
    return Settings(token, admins, os.environ.get('DATABASE_URL', 'postgresql://adbot@/auto_ad?host=/var/run/postgresql'),
                    os.environ.get('TZ', 'Asia/Shanghai'), int(os.environ.get('POLL_TIMEOUT', '25')),
                    int(os.environ.get('WORKERS', '8')))


class Redact(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{25,}\b', '[BOT_TOKEN]', message)
        record.args = ()
        return True


def configure_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    for handler in logging.getLogger().handlers:
        handler.addFilter(Redact())
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
