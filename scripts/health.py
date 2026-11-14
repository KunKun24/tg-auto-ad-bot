#!/usr/bin/env python3
"""Read-only SQL service health check. Run with the project's venv as adbot."""
from __future__ import annotations
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from adbot.config import load_settings
from adbot.db import Database


async def main():
    db=Database(load_settings().database_url)
    await db.open()
    try:
        result={'checked_at':datetime.now(timezone.utc).isoformat()}
        healthy=True
        for key,limit in [('bot_heartbeat',100),('worker_heartbeat',120)]:
            value=await db.fetchval('SELECT value FROM app_settings WHERE key=$1',key)
            age=None
            if value:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(value)).total_seconds()
            result[key]={'age_seconds':round(age,1) if age is not None else None,'healthy':age is not None and age<limit}
            healthy &= result[key]['healthy']
        result['users']=await db.fetchval('SELECT count(*) FROM users')
        result['running_tasks']=await db.fetchval("SELECT count(*) FROM tasks WHERE status='RUNNING' AND NOT deleted")
        result['unknown_deliveries']=await db.fetchval("SELECT count(*) FROM deliveries WHERE status='UNKNOWN'")
        result['unknown_broadcast_recipients']=await db.fetchval("SELECT count(*) FROM broadcast_recipients WHERE status='UNKNOWN'")
        result['pending_updates']=await db.fetchval("SELECT count(*) FROM updates WHERE status='PENDING'")
        result['error_updates']=await db.fetchval("SELECT count(*) FROM updates WHERE status='ERROR'")
        result['oldest_pending_update_seconds']=await db.fetchval("SELECT extract(epoch FROM now()-min(received_at))::float FROM updates WHERE status='PENDING'")
        result['database_bytes']=await db.fetchval('SELECT pg_database_size(current_database())')
        result['migrations']=[r['name'] for r in await db.fetch('SELECT name FROM schema_migrations ORDER BY name')]
        result['healthy']=bool(healthy)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if healthy else 1
    finally:
        await db.close()


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
