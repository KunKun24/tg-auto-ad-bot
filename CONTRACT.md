# 自动广告机器人实现约定

**中文** · [English](CONTRACT.en.md)

设计规模：约 2000 用户 / 10000+ 目标。Python 3.12、python-telegram-bot 22.8、asyncpg、PostgreSQL 16。无 Web。机器人进程持久化 getUpdates，Worker 独立运行。SQL 永久保存内容/版本/原始 update/日志。禁止把 Token 放代码/日志/测试。

本文档是模块之间的接口约定，用于说明为什么要这样拆分、每个模块对外必须提供什么。它不是教程，阅读顺序建议：先看 [README](README.md) 与 [功能介绍](docs/功能介绍.md)，再回来看这里。

## 模块职责

| 模块 | 文件 | 负责 |
| --- | --- | --- |
| 配置与基础设施 | `adbot/config.py`、`adbot/db.py`、`adbot/network.py` | 环境变量校验、连接池、SQL 迁移、日志脱敏、API 域名固定解析 |
| 主流程 | `adbot/main.py`、`adbot/worker.py` | update 持久化与串行分发、启动自检、Worker 调度循环、单实例锁 |
| 业务引擎 | `adbot/engine.py` | 任务状态机、幂等、限速、恢复、UNKNOWN 处理、广播 |
| 交互界面 | `adbot/ui.py`、`adbot/billing_ui.py` | 私聊菜单、向导、确认、管理员后台、收费管理 |
| 内容 | `adbot/content.py` | 内容捕获、entities、按钮解析、发送适配器、emoji 降级、输入解析 |
| 权益与支付 | `adbot/entitlements.py`、`adbot/payments.py` | 会员额度计算、订单状态机、签名校验、查单 |

跨模块只允许调用本文档列出的公开接口；新增依赖先评估影响面，不要直接改对方的私有实现。

## Settings / DB
`Settings` dataclass字段：token:str, admin_ids:tuple[int,...], database_url:str, timezone:str='Asia/Shanghai', poll_timeout:int=25, workers:int=8。`settings = load_settings()`从项目.env读取。
`Database(dsn)`；async `open()` / `close()`；属性`pool:asyncpg.Pool`；async `execute(sql,*args)->str`、`fetch(sql,*args)->list[Record]`、`fetchrow(sql,*args)->Record|None`、`fetchval(sql,*args)`。JSON/JSONB传Python对象（codec已设置），时间列为TIMESTAMPTZ传aware datetime。`async with db.pool.acquire() as con: async with con.transaction(): ...`。
`async audit(db, actor_id:int, action:str, resource_type:str='', resource_id:str='', details:dict|None=None)`。

## content.py 必须提供
- `parse_interval(value:str)->int`（分钟数或H:MM，最小60秒）。
- `parse_chat_ref(value:str)->int|str`（数字/@用户名/t.me/username；拒绝邀请链接与消息链接作目标）。
- `parse_source_link(value:str)->tuple[int|str,int]`（公开/私密t.me/c/链接，清理查询参数）。
- `capture_message(message:telegram.Message)->dict`：统一payload，type为text/photo/video/animation/document，text、entities（list dict）、file_id、buttons（list rows dict）、protect_content:bool；保留收到的custom_emoji实体及UTF16位置。保留合法URL按钮/颜色/图标，拒绝不安全callback按钮继承。
- `parse_buttons(text:str, entities:list[dict]|None=None)->list[list[dict]]`，#p/#r/#g、文字 - URL、同行&&，每行<=10，总<=100；按钮文字中收到的会员表情转icon_custom_emoji_id；支持文本显式 {emoji:ID}；严格报行号/URL错误。
- `build_markup(rows)->InlineKeyboardMarkup|None`
- `strip_custom_emoji(payload:dict)->dict`（仅移除custom_emoji实体和按钮icon字段，保留原fallback文字及其他实体）。
- `async deliver(bot:telegram.Bot,chat_id:int,payload:dict,*,allow_fallback:bool=True)->dict` 返回`{'message_id':int,'degraded':bool,'link':str|None}`。type=forward/copy时payload有source_chat_id/source_message_id；forward无自定义按钮；copy支持buttons。发送捕获Telegram BadRequest中明确custom_emoji资格错误才fallback重试，不可对Timeout/NetworkError自动重试（交engine处理）。使用原生entities/caption_entities；HTML/Markdown可有parse_mode字段但不可同时传entities。不能丢失custom emoji。颜色用原生style。
- `async discover_icons(bot)->dict[str,str]`：通过官方公开custom emoji sticker set或已知官方示例ID getCustomEmojiStickers核验，找menu/chats/content/tasks/stats/settings/help/admin/broadcast等图标；不可编造，失败允许普通emoji。主代理启动时持久化结果到app_settings(key='icons',value=dict)。
- `menu_button(text,callback_data=None,url=None,style=None,icon_id=None)->InlineKeyboardButton`。

## UI 契约
`class BotUI: __init__(self,db:Database,bot:telegram.Bot,settings:Settings)`；async `handle_update(update:telegram.Update)->None`。main自管update持久化/重放/逐用户串行，不使用Application Dispatcher。UI处理message(私聊含commands/会话输入)、callback_query、my_chat_member。channel_post由engine接收，UI不用处理来源触发。
UI通过`from .engine import Engine`，实例`Engine(db,bot,settings)`调用下面公开API，勿自行复写调度规则。
`await engine.create_task(owner_id:int,name:str,content_ids:list[int],chat_ids:list[int],interval_seconds:int,delete_previous:bool=False,instant_source_id:int|None=None,source_mode:str='copy',max_runs:int=0,skip_failed:bool=False,delete_failure_pause:bool=False)->int` 创建DRAFT，content_ids是contents.id（即时来源可以配轮播内容；必须至少一条）；后续`start_task`。
`await engine.start_task(task_id:int,owner_id:int)->None`（DRAFT/STOPPED首发立即，PAUSED剩余等待，ERROR需先解决unknown）；async `pause_task`、`stop_task`、`delete_task`（软删除保留历史）、`run_now`、`resolve_unknown(task_id,owner_id,action:str)`（action=retry/skip）、`remove_source(task_id,owner_id)`；UI负责用户可见的二次确认及管理员代理owner时audit。
`await engine.create_broadcast(admin_id:int,payload:dict)->int`（所有已启动未封禁/未block用户快照），`broadcast_action(job_id,admin_id,action)`（pause/resume/cancel），`broadcast_stats(job_id)->dict`；UI创建后发进度消息并更新broadcast_jobs.progress_chat_id/progress_message_id供Worker动态编辑。
`await engine.schedule_test_delete(owner_id,chat_id,message_id,delay=30)`。
`await engine.ingest_source(message:Message, update_id:int)`由main处理channel_post与已授权源群message；消息永久索引；仅RUNNING任务新消息触发。暂停期间不积压；定时与即时同锁。来源=目标/循环来源关系禁止。

## SQL schema （UI按精确字段使用）
所有id BIGSERIAL（users.id/chat_id为Telegram BIGINT），时间默认now()。
users(id PK,username,first_name,last_name,language_code,status TEXT default ACTIVE,blocked BOOL default false,timezone TEXT default Asia/Shanghai,first_seen TIMESTAMPTZ,last_seen TIMESTAMPTZ,ban_reason TEXT default '')。
chats(chat_id PK,type TEXT,title TEXT,username TEXT,bot_status TEXT default UNKNOWN,permissions JSONB default {},updated_at TIMESTAMPTZ)。
user_chats(id PK,owner_id FK users,chat_id FK chats,active BOOL default true,authorized_at TIMESTAMPTZ,UNIQUE(owner_id,chat_id))。解绑仅active=false，不删除现有task_targets。
sources(id PK,owner_id FK users,chat_id BIGINT,title TEXT,username TEXT,active BOOL default true,authorized_at TIMESTAMPTZ,UNIQUE(owner_id,chat_id))。解绑active=false，只禁止新选择，不停已有任务，真正停止需engine.remove_source。
source_messages(id PK,chat_id BIGINT,message_id BIGINT,payload JSONB,received_at TIMESTAMPTZ,UNIQUE(chat_id,message_id))。查询必须JOIN该owner的sources授权；管理员可看所有并审计。
contents(id PK,owner_id FK users,title TEXT,current_version_id BIGINT nullable,deleted BOOL false,created_at,updated_at)。
content_versions(id PK,content_id FK contents,version_no INT,payload JSONB,created_at,UNIQUE(content_id,version_no))。更改内容新建version；UI事务更新current_version_id，旧版本不修改。copy/forward是引用；普通捕获type=...是快照。版本应用任务由重新保存/任务编辑实现，每次事件固定version。
sessions(owner_id PK,state TEXT,data JSONB,expires_at TIMESTAMPTZ,updated_at TIMESTAMPTZ)。15min无操作超时，取消/返回清空。会话state/data可由UI自定。
tasks(id PK,owner_id,name,status TEXT default DRAFT,interval_seconds INT,next_run_at TIMESTAMPTZ,remaining_seconds INT default0,current_index INT default0,delete_previous BOOL false,instant_source_id BIGINT nullable FK sources,source_mode TEXT default copy,max_runs INT default0,completed_runs INT default0,skip_failed BOOL false,delete_failure_pause BOOL false,deleted BOOL false,last_error TEXT default '',created_at,updated_at)。
task_items(task_id FK tasks,position INT,content_id FK contents,PRIMARY KEY(task_id,position))。
task_targets(id PK,task_id FK tasks,chat_id BIGINT,last_message_id BIGINT nullable,last_sent_at TIMESTAMPTZ,UNIQUE(task_id,chat_id))。
publication_events(id PK,task_id FK tasks,kind TEXT [SCHEDULED/INSTANT/MANUAL],dedupe_key TEXT UNIQUE,payload JSONB,status TEXT default PENDING,scheduled_at TIMESTAMPTZ default now(),started_at,finished_at)。
deliveries(id PK,event_id FK publication_events,target_id FK task_targets,chat_id BIGINT,status TEXT default PENDING [PENDING/SENDING/RETRY/SUCCESS/FAILED/UNKNOWN/SKIPPED],attempts INT default0,next_attempt_at TIMESTAMPTZ default now(),message_id BIGINT nullable,error TEXT default '',degraded BOOL false,delete_status TEXT,delete_error TEXT,started_at,finished_at,UNIQUE(event_id,target_id))。
message_logs(id PK,owner_id,task_id nullable,event_id nullable,chat_id,message_id nullable,operation TEXT [SEND/DELETE/TEST],status TEXT [SUCCESS/FAILED/UNKNOWN],error TEXT default '',degraded BOOL false,payload_version_id BIGINT nullable,created_at)。
broadcast_jobs(id PK,admin_id,payload JSONB,status TEXT default RUNNING [RUNNING/PAUSED/CANCELLED/COMPLETED/ERROR],progress_chat_id BIGINT nullable,progress_message_id BIGINT nullable,last_progress_at TIMESTAMPTZ,created_at,updated_at)。
broadcast_recipients(id PK,job_id FK broadcast_jobs,user_id,status TEXT default PENDING [PENDING/SENDING/RETRY/SUCCESS/FAILED/UNKNOWN/SKIPPED],attempts INT default0,next_attempt_at TIMESTAMPTZ default now(),message_id BIGINT nullable,error TEXT default '',degraded BOOL false,started_at,finished_at,UNIQUE(job_id,user_id))。
audit_logs(id PK,actor_id BIGINT,action TEXT,resource_type TEXT default '',resource_id TEXT default '',details JSONB default {},created_at)。
app_settings(key TEXT PK,value JSONB,updated_at)。
updates(update_id BIGINT PK,payload JSONB,status TEXT default PENDING,attempts INT default0,error TEXT default '',received_at,processed_at)。
deferred_deletions(id PK,owner_id,chat_id,message_id,run_at TIMESTAMPTZ,status TEXT default PENDING,error TEXT default '',UNIQUE(chat_id,message_id))。

## 行为约定
绑定/来源首次get_chat + get_chat_member(user)管理权 + bot管理员/频道post权限；用户失去管理员或解绑后已存任务继续（用户明确要求），不自动撤销，界面需提示。每次真正发送前仍核验bot当前权限。ban暂停全用户任务；unban不自动启动。私密索引仅曾授权的owner或admin可读。管理员查看别人正文审计。
轮播间隔最小60s，输入50=3000s/1:30=5400s。完成一批全部成功后游标推进，next_run_at=完成时间+interval。部分成功重试仅失败项；5次上限/永久错误暂停ERROR；skip_failed开则跳过失败推进。Unknown不自动重试。暂停保留批次可恢复，已在途可能返回；取消queued instant，不接受暂停源触发。崩溃残留SENDING转UNKNOWN并暂停任务/广播。来源每条逐条排队1000上限，超限暂停并告警，不静默丢。防止source=target/跨任务循环。发新前删旧；删除失败默认继续，开暂停则ERROR。相册为文档允许的后续扩展，当前至少五种媒体完整。
管理员：总览、用户搜索与详情/封禁解封二次确认/查看全部内容任务目标日志及审计、配置（最小间隔/配额/测试消息/联系管理员）、广播完整创建预览确认/进度条/暂停继续取消/失败查看导出/未知处理。普通用户：目标批量绑定检测/测试/解绑、内容创建版本/编辑预览/删除/保护/格式/按钮、来源绑定/导入链接/索引浏览、任务向导选多内容多目标/间隔/删旧/即时来源/有限轮次/失败跳过/删失败暂停/预览确认、任务启动暂停恢复停止删除手动发布与unknown处置、统计发送删除/失败记录/导出/设置/教程。
