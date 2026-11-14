#!/usr/bin/env python3
"""Create and validate a PostgreSQL custom-format backup. Run as adbot."""
from __future__ import annotations
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from adbot.config import load_settings


def connection():
    settings=load_settings()
    parsed=urlparse(settings.database_url)
    query=parse_qs(parsed.query)
    env=dict(os.environ)
    if parsed.password:
        env['PGPASSWORD']=unquote(parsed.password)
    args=['-h',query.get('host',[parsed.hostname or '/var/run/postgresql'])[0],'-U',unquote(parsed.username or 'adbot')]
    if parsed.port:
        args+=['-p',str(parsed.port)]
    return args,unquote(parsed.path.lstrip('/')),env


def main():
    os.umask(0o077)
    destination=ROOT/'backups'
    destination.mkdir(mode=0o700,exist_ok=True)
    args,database,env=connection()
    name=destination/f'{database}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.dump'
    subprocess.run(['pg_dump',*args,'-d',database,'-Fc','--no-owner','--no-acl','-f',str(name)],check=True,env=env)
    subprocess.run(['pg_restore','--list',str(name)],check=True,stdout=subprocess.DEVNULL)
    print(f'备份完成并通过目录校验：{name} ({name.stat().st_size} bytes)')


if __name__=='__main__':
    main()
