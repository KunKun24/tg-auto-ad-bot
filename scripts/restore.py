#!/usr/bin/env python3
"""Restore to an explicitly named empty database; never overwrite configured DB."""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.backup import connection


def main():
    parser=argparse.ArgumentParser(description='恢复到显式指定的空数据库，不覆盖正在配置的数据库')
    parser.add_argument('dump',type=Path)
    parser.add_argument('--target',required=True)
    opts=parser.parse_args()
    args,production,env=connection()
    if opts.target==production or not opts.target or not opts.dump.is_file():
        parser.error('目标必须与当前DATABASE_URL不同，备份文件必须存在')
    count=subprocess.check_output(['psql',*args,'-d',opts.target,'-At','-v','ON_ERROR_STOP=1','-c',"SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"],env=env,text=True).strip()
    if count!='0':
        parser.error('目标数据库必须为空；工具不清理或覆盖已有表')
    subprocess.run(['pg_restore',*args,'-d',opts.target,'--exit-on-error','--single-transaction','--no-owner','--no-acl',str(opts.dump.resolve())],check=True,env=env)
    print(f'已恢复至隔离数据库 {opts.target}；检查内容并停止服务后，才可手动切换DATABASE_URL。')


if __name__=='__main__':
    main()
