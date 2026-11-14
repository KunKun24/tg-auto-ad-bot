"""Membership and payment UI. Secret inputs never enter ordinary content sessions."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from telegram import InlineKeyboardMarkup

from .content import menu_button

SECRET_REDACTED = '[BILLING_SECRET_REDACTED]'
TERMS = ('购买前请确认：订阅按付款成功的顺序分段排队，不提前改变当前套餐；'
         '付款不会自动恢复任务，到期暂停，不补发过期期间的即时消息。\n'
         '正常付款不退款；法律或平台强制要求、重复扣款及未履行服务的纠错除外。\n'
         'Telegram 数字服务可能受 Stars 政策限制；OKPay 为第三方渠道，不代表获 Telegram 认可或合规，'
         '存在平台限制、支付和服务中断风险。\n'
         '订单权益固定归本机器人当前用户；他人代付不改变权益所属人。')
PLAN_NAMES = {'week': '周会员', 'month': '月会员', 'quarter': '季会员'}


def _when(value):
    if not value:
        return '无'
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return str(value)[:40]


def _pay_url(value):
    """Share the payment boundary's strict official receipt-link validator."""
    from .payments import PaymentError, normalize_pay_url
    try:
        return normalize_pay_url(value)
    except PaymentError:
        return None


class BillingUIMixin:
    def init_billing(self):
        from .entitlements import Entitlements
        from .payments import Payments
        self.entitlements = Entitlements(self.db, self.settings)
        self.payments = Payments(self.db, self.settings)

    async def billing_snapshot(self, uid):
        return await self.entitlements.snapshot(uid)

    async def billing_consume(self, uid, state, nonce):
        session = await self.state(uid)
        if not session or session['state'] != state or session['data'].get('nonce') != nonce:
            raise ValueError('此确认已过期或已使用，请重新开始。')
        consumed = await self.db.fetchrow('DELETE FROM sessions WHERE owner_id=$1 AND state=$2 '
                                          "AND data->>'nonce'=$3 AND expires_at>now() RETURNING owner_id",
                                          uid, state, nonce)
        if not consumed:
            raise ValueError('此确认已过期或已使用，请重新开始。')
        return session['data']

    async def billing_confirm(self, uid, state, data, text, query=None):
        data = await self.set_state(uid, state, data)
        await self.screen(uid, text, [[('确认', f"badmin:confirm:{data['nonce']}", 'danger'),
                                      ('取消', 'badmin:home')]], query, back='badmin:home')

    async def billing_membership(self, uid, query=None, owner=None):
        owner = uid if owner is None else owner
        if owner != uid:
            self.require_admin(uid)
        snap = await self.billing_snapshot(owner)
        plan = '管理员永久豁免' if snap['exempt'] else PLAN_NAMES.get(snap.get('plan_code'), '未生效会员')
        limit = '无限' if snap.get('channel_limit') is None else str(snap['channel_limit'])
        text = (f"会员中心 · 用户 {owner}\n当前：{plan}\n"
                f"收费状态：{'已启用' if snap['enabled'] else '尚未启用'}\n"
                f"发布资格：{'有效' if snap['active'] or snap['exempt'] else '已到期 / 尚未购买'}\n"
                f"当前开始：{_when(snap.get('starts_at'))}\n当前到期：{_when(snap.get('expires_at'))}\n"
                f"排队订阅截止：{_when(snap.get('queued_until'))}\n"
                f"永久附加目标：{snap['extra_channels']}\n已用目标 / 上限：{snap['used_channels']} / {limit}\n"
                '目标是频道与群组的去重并集：有效绑定和未删除任务的有效目标均计算，来源不计算。\n'
                '到期或超额会暂停发布；请解绑并移除多余任务目标后手动恢复。付款后也须手动启动，不删历史数据。')
        if owner == uid:
            rows = [[('购买 / 续费', 'bpay:shop', 'success'), ('我的订单', 'bpay:orders:0', 'primary')],
                    [('查看任务', 'list:tasks:0'), ('联系支持', 'contact')]]
        else:
            rows = [[('调整权益', f'badmin:adjust:{owner}', 'danger'), ('用户订单', f'badmin:orders:0:{owner}')]]
        await self.screen(uid, text, rows, query, back='home' if owner == uid else 'badmin:home')

    async def billing_shop(self, uid, query=None):
        snap = await self.billing_snapshot(uid)
        rows = [[('周 · 7天 / 10 USDT / 20目标', 'bpay:buy:week', 'primary'),
                 ('月 · 30天 / 30 USDT / 无限', 'bpay:buy:month', 'success')],
                [('季 · 90天 / 66 USDT / 无限', 'bpay:buy:quarter', 'success'), ('我的订单', 'bpay:orders:0')]]
        if snap['active'] and not snap['exempt'] and snap.get('plan_code') == 'week':
            rows.append([('永久 +10目标 / 3 USDT', 'bpay:buy:extra10', 'primary'),
                         ('永久 +50目标 / 10 USDT', 'bpay:buy:extra50', 'primary')])
        text = ('会员购买 / 续费\n周：7天，10 USDT，20个目标。\n月：30天，30 USDT，目标不限。\n'
                '季：90天，66 USDT，目标不限。\n附加包：+10 / 3 USDT；+50 / 10 USDT。永久叠加、不增加会员时间，'
                '仅当前有效周会员可购买（管理员无需购买）。\n商业目标不限不取消 Telegram 限速、安全限制或权限要求。\n\n' + TERMS)
        await self.screen(uid, text, rows, query, back='bpay:member')

    async def billing_purchase(self, uid, code, query=None):
        from .payments import PRODUCTS
        if code not in PRODUCTS:
            raise ValueError('未知商品。')
        snap = await self.billing_snapshot(uid)
        if snap['exempt']:
            raise ValueError('管理员已永久豁免，无需购买。')
        product = PRODUCTS[code]
        if code.startswith('extra') and not (snap['active'] and snap.get('plan_code') == 'week'):
            raise ValueError('附加目标包仅限当前有效周会员购买。')
        activation = ('永久增加目标额度，不增加会员时间。' if code.startswith('extra') else
                      f"生效：当前已购分段结束后依序开始；预计不早于 {_when(snap.get('queued_until') or datetime.now(timezone.utc))}。\n"
                      f"本次完整增加 {product['days']} 天，不截断当前套餐。最终时间以付款入账时的队列为准。")
        data = await self.set_state(uid, 'BILLING_PURCHASE', {'product_code': code, 'request_key': secrets.token_hex(16)})
        await self.screen(uid, f"确认购买：{product['title']}\n金额：{product['amount']} USDT\n{activation}\n\n{TERMS}",
                          [[('同意条款并创建订单', f"bpay:confirm:{data['nonce']}", 'success'),
                            ('取消购买', 'bpay:shop', 'danger')]], query, back='bpay:shop')

    async def billing_order(self, uid, order, query=None, *, admin=False):
        if admin:
            self.require_admin(uid)
        elif int(order['owner_id']) != uid:
            raise PermissionError('订单不属于你。')
        oid = int(order['id'])
        status = order['status']
        link_review = status == 'REVIEW' and order.get('error') == '支付链接异常，请联系管理员'
        names = {'CREATING': '创建处理中', 'PENDING': '等待付款', 'CREATE_UNKNOWN': '创建结果自动核实中，请勿重复购买',
                 'PAID': '已付款 / 已入账', 'REVIEW': '支付入口自动恢复中' if link_review else '支付数据校验异常', 'FAILED': '创建失败'}
        text = (f"订单 #{oid}\n所属用户：{order['owner_id']}\n商品：{order['product_code']}\n"
                f"商户订单号：{order.get('unique_id') or '生成中'}\n"
                f"OKPay 平台订单号：{order.get('provider_order_id') or '自动获取中'}\n"
                f"金额：{order['amount']} {order.get('coin', 'USDT')}\n状态：{names.get(status, status)}\n"
                f"创建：{_when(order.get('created_at'))}\n付款：{_when(order.get('paid_at'))}\n"
                '付款后系统自动检测并开通；查询仅刷新结果，不需管理员手动确认。\n'
                '请勿重复付款。代付不会改变权益所属用户。\n'
                '购买成功不会自动恢复任务，请前往任务手动启动。')
        if link_review:
            text += '\n系统正在自动查单恢复原付款入口，无需人工确认到账；请稍候点击查询付款结果，勿重复下单。'
        elif status == 'REVIEW':
            text += '\n支付数据未通过安全校验，暂未发放权益，请联系支持处理；请勿重复付款。'
        if not order.get('pay_url') and status in {'CREATING', 'CREATE_UNKNOWN', 'PENDING'}:
            text += '\n原订单正在核实，支付链接可能遗失。请保留原单并联系管理员，勿重复下单或付款。'
        action = f'badmin:check:{oid}' if admin else f'bpay:check:{oid}'
        rows = [[('查询付款结果', action, 'primary'), ('联系支持', 'contact')]]
        url = _pay_url(order.get('pay_url'))
        if url and status == 'PENDING':
            self.icons = await self.config('icons', {}) or {}
            markup = self.markup(uid, rows, 'badmin:home' if admin else 'bpay:orders:0')
            pay = menu_button('前往 OKPay 付款', url=url, style='success', icon_id=self.icons.get('menu'))
            markup = InlineKeyboardMarkup([[pay], *markup.inline_keyboard])
            # screen owns throttling, fallback and safe editing; URL has already been allowlisted.
            await self.screen(uid, text, rows, query, back='badmin:home' if admin else 'bpay:orders:0', markup=markup)
        else:
            await self.screen(uid, text, rows, query, back='badmin:home' if admin else 'bpay:orders:0')

    async def billing_orders(self, uid, page=0, query=None, owner=None):
        owner = uid if owner is None else owner
        admin = owner != uid
        if admin:
            self.require_admin(uid)
        page = max(0, page)
        records = await self.payments.list_orders(owner, limit=9, offset=page * 8)
        prefix = 'badmin' if admin else 'bpay'
        rows = []
        for offset in range(0, min(len(records), 8), 2):
            rows.append([(f"#{r['id']} {r['product_code']} · {r['status']}", f"{prefix}:detail:{r['id']}")
                         for r in records[offset:offset + 2]])
        suffix = f':{owner}' if admin else ''
        nav = []
        if page:
            nav.append(('上一页', f'{prefix}:orders:{page - 1}{suffix}'))
        if len(records) > 8:
            nav.append(('下一页', f'{prefix}:orders:{page + 1}{suffix}'))
        if nav:
            rows.append(nav)
        await self.screen(uid, f'用户 {owner} 的订单 · 第{page + 1}页\n' + ('暂无订单。' if not records else '选择订单查看状态；查询不会创建新订单。'),
                          rows, query, back='badmin:home' if admin else 'bpay:member')

    async def billing_callback(self, uid, p, query):
        if p[0] not in {'bpay', 'badmin'}:
            return False
        if p[0] == 'badmin':
            self.require_admin(uid)
            await self.billing_admin_callback(uid, p[1:], query)
            return True
        verb = p[1] if len(p) > 1 else 'member'
        if verb == 'member':
            await self.clear(uid)
            await self.billing_membership(uid, query)
        elif verb == 'shop':
            await self.clear(uid)
            await self.billing_shop(uid, query)
        elif verb == 'buy' and len(p) == 3:
            await self.billing_purchase(uid, p[2], query)
        elif verb == 'confirm' and len(p) == 3:
            session = await self.state(uid)
            if not session or session['state'] != 'BILLING_PURCHASE' or session['data'].get('nonce') != p[2]:
                raise ValueError('此购买确认已失效，请重新选择套餐。')
            data = session['data']
            # Preserve the confirmed request key so races/retries resolve the same durable order.
            order = await self.payments.create_order(uid, data['product_code'], data['request_key'])
            await self.billing_order(uid, order, query)
        elif verb in {'detail', 'check'} and len(p) == 3:
            if verb == 'check':
                await self.screen(uid, '正在核验付款结果，请稍候；请勿重复付款。', query=query, back='bpay:orders:0')
                order = await self.payments.check_order(int(p[2]), owner_id=uid)
            else:
                order = await self.db.fetchrow('SELECT * FROM billing_orders WHERE id=$1 AND owner_id=$2', int(p[2]), uid)
                if not order:
                    raise PermissionError('订单不存在或不属于你。')
            await self.billing_order(uid, order, query)
        elif verb == 'orders' and len(p) == 3:
            await self.billing_orders(uid, int(p[2]), query)
        else:
            raise ValueError('收费菜单已失效，请重新打开会员中心。')
        return True

    async def billing_admin_callback(self, uid, p, query):
        self.require_admin(uid)
        verb = p[0] if p else 'home'
        if verb == 'home':
            await self.clear(uid)
            summary = await self.payments.config_summary(uid)
            enabled = bool(await self.config('billing_enabled', False))
            stats = await self.db.fetch('SELECT status,count(*) AS n FROM billing_orders GROUP BY status')
            text = (f"收费管理\n收费：{'启用' if enabled else '关闭'}\n"
                    f"收款配置：{'已设置' if summary['configured'] else '未设置'}\n商户 ID：{summary.get('merchant_id') or '无'}\n"
                    f"密钥：{summary.get('masked_token') or '未设置'}\n"
                    '更换凭据保留历史订单凭据；平台停用旧密钥可能导致历史订单无法核验。\n' +
                    '\n'.join(f"{r['status']}：{r['n']}" for r in stats))
            await self.screen(uid, text, [[('收款配置', 'badmin:config', 'primary'),
                                          ('关闭收费' if enabled else '启用收费', f'badmin:switch:{int(not enabled)}', 'danger')],
                                         [('查询用户权益 / 订单', 'badmin:lookup'), ('返回管理', 'admin')]], query, back='admin')
        elif verb == 'switch' and len(p) == 2 and p[1] in {'0', '1'}:
            await self.billing_confirm(uid, 'BILLING_SWITCH', {'enabled': p[1] == '1'},
                                       ('确认启用收费？会暂停所有非管理员运行任务，保留数据、游标和剩余等待；付款后不自动恢复。'
                                        if p[1] == '1' else '确认关闭收费？不会自动启动任何已暂停任务。'), query)
        elif verb == 'config':
            await self.prompt(uid, 'BILLING_CONFIG_MERCHANT', '输入 OKPay 商户 ID（不是密钥）。后续确认风险后才会开启专用密钥输入。', {}, query)
        elif verb == 'confirm' and len(p) == 2:
            session = await self.state(uid)
            state = session['state'] if session else ''
            if state not in {'BILLING_SWITCH', 'BILLING_CONFIG_WARNING', 'BILLING_ADJUST_CONFIRM'}:
                raise ValueError('确认已失效。')
            data = await self.billing_consume(uid, state, p[1])
            if state == 'BILLING_SWITCH':
                await self.entitlements.set_enabled(uid, data['enabled'])
                await self.billing_admin_callback(uid, ['home'], query)
            elif state == 'BILLING_CONFIG_WARNING':
                await self.prompt(uid, 'BILLING_CONFIG_SECRET',
                                  '专用密钥输入已开启。仅发送密钥，或 /paytoken 密钥。收到后会立即尽力删除消息；不要附带其他正文。'
                                  '\n若进程重启导致只剩脱敏记录，请重新发送。/cancel 退出，旧配置保持不变。',
                                  {'merchant_id': data['merchant_id']}, query)
            else:
                await self.entitlements.adjust(uid, data['owner_id'], days=data['days'], extra_channels=data['extra_channels'], reason=data['reason'])
                await self.billing_membership(uid, query, data['owner_id'])
        elif verb == 'lookup':
            await self.prompt(uid, 'BILLING_LOOKUP', '输入需要查询的用户数字 ID。', {}, query)
        elif verb == 'member' and len(p) == 2:
            await self.billing_membership(uid, query, int(p[1]))
        elif verb == 'adjust' and len(p) == 2:
            owner = int(p[1])
            if not await self.db.fetchrow('SELECT id FROM users WHERE id=$1', owner):
                raise ValueError('用户不存在。')
            await self.prompt(uid, 'BILLING_ADJUST', '输入：天数 附加目标数 原因\n例如：7 10 补偿服务中断\n'
                              '允许负数扣减，额度不能低于0；正天数按无限目标月会员分段追加。必须填写原因。', {'owner_id': owner}, query)
        elif verb == 'orders' and len(p) == 3:
            await self.billing_orders(uid, int(p[1]), query, int(p[2]))
        elif verb in {'detail', 'check'} and len(p) == 2:
            if verb == 'check':
                await self.screen(uid, '正在核验订单，请稍候。', query=query, back='badmin:home')
                order = await self.payments.check_order(int(p[1]), admin_id=uid)
            else:
                order = await self.db.fetchrow('SELECT * FROM billing_orders WHERE id=$1', int(p[1]))
                if not order:
                    raise ValueError('订单不存在。')
            await self.billing_order(uid, order, query, admin=True)
        else:
            raise ValueError('收费管理菜单已失效。')

    async def billing_delete_secret(self, uid, message):
        try:
            await self.bot.delete_message(chat_id=uid, message_id=message.message_id)
        except Exception:
            # Never put Telegram errors or the secret-bearing message into audit/log/UI.
            pass

    async def billing_message(self, uid, message, session, *, force_secret=False):
        text = message.text or message.caption or ''
        command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
        state = session['state'] if session else ''
        if SECRET_REDACTED in text:
            await self.billing_delete_secret(uid, message)
            await self.screen(uid, '此消息已脱敏，不能作为正文或密钥使用；请重新打开收费配置并发送。')
            return True
        if force_secret or command == '/paytoken' or state == 'BILLING_CONFIG_SECRET':
            if command in {'/cancel', '/start', '/menu', '/help', '/admin'}:
                await self.clear(uid)
                return False
            await self.billing_delete_secret(uid, message)
            self.require_admin(uid)
            if state != 'BILLING_CONFIG_SECRET':
                await self.screen(uid, '请先在收费管理中输入商户 ID 并确认风险；本消息已尽力删除，未保存密钥。')
                return True
            if SECRET_REDACTED in text or not text.strip():
                await self.screen(uid, '此消息已脱敏或没有密钥，请在专用密钥输入状态重新发送。')
                return True
            token = text.split(maxsplit=1)[1].strip() if command == '/paytoken' and len(text.split(maxsplit=1)) == 2 else text.strip()
            if command == '/paytoken' and token == '/paytoken':
                await self.screen(uid, '请发送非空密钥。')
                return True
            data = await self.billing_consume(uid, state, session['data']['nonce'])
            try:
                await self.payments.configure(uid, data['merchant_id'], token)
            except Exception:
                await self.screen(uid, '配置验证失败，旧配置未变。请检查凭据和加密配置后，从收费管理重新开始。')
                return True
            finally:
                token = None
            await self.screen(uid, '凭据已通过非资金验证并启用。消息已尽力删除，界面不显示密钥。',
                              [[('收费管理', 'badmin:home', 'primary'), ('返回管理', 'admin')]])
            return True
        if not state.startswith('BILLING_'):
            return False
        if command in {'/cancel', '/start', '/menu', '/help', '/admin'}:
            return False
        if state == 'BILLING_PURCHASE':
            await self.screen(uid, '请使用购买确认按钮，或 /cancel 取消后重新选择。', back='bpay:shop')
            return True
        self.require_admin(uid)
        data = session['data']
        if state == 'BILLING_CONFIG_MERCHANT':
            merchant = text.strip()
            if not re.fullmatch(r'[0-9]{1,32}', merchant):
                raise ValueError('请输入1到32位数字商户 ID；此步骤不要发送密钥。')
            await self.billing_confirm(uid, 'BILLING_CONFIG_WARNING', {'merchant_id': merchant},
                                       f'商户 ID：{merchant}\n确认后进入专用密钥输入。机器人会以余额查询验证凭据，不转账。'
                                       '\n密钥由服务端加密保存；Telegram 消息删除不能保证平台或客户端副本被清除。'
                                       '\n轮换会影响历史订单：旧加密凭据保留，但平台停用旧密钥可能使旧订单无法核验。是否继续？')
        elif state == 'BILLING_LOOKUP':
            if not text.strip().isdigit() or len(text.strip()) > 19:
                raise ValueError('请输入有效数字用户 ID。')
            owner = int(text.strip())
            if not await self.db.fetchrow('SELECT id FROM users WHERE id=$1', owner):
                raise ValueError('用户不存在。')
            await self.clear(uid)
            await self.billing_membership(uid, owner=owner)
        elif state == 'BILLING_ADJUST':
            parts = text.strip().split(maxsplit=2)
            if len(parts) != 3 or not all(re.fullmatch(r'-?\d{1,6}', v) for v in parts[:2]) or not 1 <= len(parts[2]) <= 300:
                raise ValueError('格式：天数 附加目标数 原因（1到300字）。')
            days, extra = int(parts[0]), int(parts[1])
            if not (days or extra):
                raise ValueError('至少调整一项。')
            if abs(days) > 3650 or abs(extra) > 100000:
                raise ValueError('单次最多调整3650天或100000个永久目标。')
            await self.billing_confirm(uid, 'BILLING_ADJUST_CONFIRM',
                                       {'owner_id': data['owner_id'], 'days': days, 'extra_channels': extra, 'reason': parts[2]},
                                       f"确认调整用户 {data['owner_id']}：天数 {days:+}，永久目标 {extra:+}\n原因：{parts[2]}\n不会自动恢复任务。")
        else:
            raise ValueError('请使用当前收费步骤的按钮，或 /cancel。')
        return True
