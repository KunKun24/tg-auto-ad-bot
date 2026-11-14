from __future__ import annotations

import json
from pathlib import Path

import asyncpg


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def open(self):
        async def init(con):
            for typ in ('json', 'jsonb'):
                await con.set_type_codec(typ, encoder=lambda x: json.dumps(x, ensure_ascii=False), decoder=json.loads, schema='pg_catalog')
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=16, init=init, command_timeout=30)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def execute(self, sql, *args):
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql, *args):
        return await self.pool.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        return await self.pool.fetchval(sql, *args)

    async def migrate(self):
        root = Path(__file__).resolve().parent.parent / 'migrations'
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock(7150000001)')
                await con.execute('CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
                for path in sorted(root.glob('*.sql')):
                    if not await con.fetchval('SELECT 1 FROM schema_migrations WHERE name=$1', path.name):
                        await con.execute(path.read_text())
                        await con.execute('INSERT INTO schema_migrations(name) VALUES($1)', path.name)


async def audit(db, actor_id: int, action: str, resource_type: str = '', resource_id: str = '', details: dict | None = None):
    await db.execute('INSERT INTO audit_logs(actor_id,action,resource_type,resource_id,details) VALUES($1,$2,$3,$4,$5)',
                     actor_id, action, resource_type, str(resource_id), details or {})
