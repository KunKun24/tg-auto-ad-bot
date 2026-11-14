"""Durable OKPay deposits. No callbacks, transfers, or automatic order recreation."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlsplit

import asyncpg
import httpx
from cryptography.fernet import Fernet, InvalidToken

from .entitlements import Entitlements


PRODUCTS = {
    "week": {"title": "周套餐", "amount": "10", "days": 7, "channels": 20, "extra": 0},
    "month": {"title": "月套餐", "amount": "30", "days": 30, "channels": None, "extra": 0},
    "quarter": {"title": "季套餐", "amount": "66", "days": 90, "channels": None, "extra": 0},
    "extra10": {"title": "永久加量10个", "amount": "3", "days": 0, "channels": None, "extra": 10},
    "extra50": {"title": "永久加量50个", "amount": "10", "days": 0, "channels": None, "extra": 50},
}
API_ROOT = "https://api.okaypay.me/shop/"
_METHODS = frozenset(("balance", "payLink", "checkDeposit", "checkTransferByTxid"))
_ORDER_LOCK_BASE = -(1 << 62)
_CONFIG_LOCK = -(1 << 63) + 1


class PaymentError(ValueError):
    """Only fixed, non-sensitive messages may leave the payment boundary."""


class _Unavailable(PaymentError):
    pass


class _Untrusted(PaymentError):
    pass


def _flatten(data, prefix=""):
    out = {}
    for key, value in data.items():
        key = str(key) if prefix == "" else f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, key))
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif value is not None and value != "":
            out[key] = str(value)
    return out


def build_base(params: dict) -> str:
    flat = _flatten({key: value for key, value in params.items() if key != "sign"})
    return "&".join(f"{key}={flat[key]}" for key in sorted(flat))


def sign(params: dict, token: str) -> str:
    return hmac.new(token.encode("utf-8"), build_base(params).encode("utf-8"), hashlib.sha256).hexdigest().upper()


def signed_request(params: dict, shop_id, token: str) -> dict:
    payload = dict(params)
    payload["id"] = shop_id
    payload["timestamp"] = int(time.time())
    payload["nonce"] = secrets.token_hex(8)
    payload["sign"] = sign(payload, token)
    return payload


def verify(payload: dict, token: str) -> bool:
    if not isinstance(payload, dict):
        return False
    got = payload.get("sign")
    if not isinstance(got, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", got):
        return False
    try:
        return hmac.compare_digest(sign(payload, token), got.upper())
    except (TypeError, ValueError, RecursionError):
        return False


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        # Dot keys can shadow nested fields during the SDK's flattening step.
        if key in result or not re.fullmatch(r"[A-Za-z0-9_]+", key) or isinstance(value, list):
            raise ValueError("ambiguous JSON fields")
        result[key] = value
    return result


def _bad_constant(_value):
    raise ValueError("invalid JSON number")


def _merchant(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,32}", str(value)):
        raise _Untrusted("支付响应商户不匹配，请联系管理员")
    return str(int(value))


def _status(data):
    value = data.get("status")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) not in ("0", "1"):
        raise _Untrusted("支付响应状态异常，请联系管理员")
    return int(value)


def _provider_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise _Untrusted("支付响应订单异常，请联系管理员")
    return value


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise _Untrusted("支付响应金额异常，请联系管理员")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError):
        raise _Untrusted("支付响应金额异常，请联系管理员") from None


def _canonical_pay_url(provider):
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", provider):
        raise _Untrusted("支付链接异常，请联系管理员")
    return "https://t.me/OkayPayBot?start=shop_deposit--" + provider


def normalize_pay_url(value):
    if (not isinstance(value, str) or len(value) > 2000 or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise _Untrusted("支付链接异常，请联系管理员")
    try:
        url = urlsplit(value)
        if url.username is not None or url.password is not None or url.fragment or url.port is not None:
            raise ValueError
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise ValueError
        if url.scheme == "https" and url.hostname == "t.me":
            if set(query) != {"start"}:
                raise ValueError
            endpoint = url.path.lower()
            if endpoint.endswith("/"):
                endpoint = endpoint[:-1]
            if endpoint not in ("/okpay", "/okaypaybot"):
                raise ValueError
            username = endpoint[1:]
        elif url.scheme == "tg" and url.netloc.lower() == "resolve" and url.path == "":
            if set(query) != {"domain", "start"}:
                raise ValueError
            username = query["domain"].lower()
            if username not in ("okpay", "okaypaybot"):
                raise ValueError
        else:
            raise ValueError
        prefix = "shop_deposit----" if username == "okpay" else "shop_deposit--"
        start = query["start"]
        if not start.startswith(prefix):
            raise ValueError
        return _canonical_pay_url(start[len(prefix):])
    except (ValueError, KeyError):
        raise _Untrusted("支付链接异常，请联系管理员") from None


def _pay_url(value):
    return normalize_pay_url(value)


def _fernet():
    try:
        return Fernet(os.environ["BILLING_ENCRYPTION_KEY"].encode("ascii"))
    except (KeyError, ValueError, UnicodeError):
        raise PaymentError("支付加密密钥未正确配置，请联系管理员") from None


def _decrypt(credential):
    try:
        return _fernet().decrypt(credential["token_encrypted"].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, AttributeError):
        raise PaymentError("历史支付配置无法解密，请联系管理员") from None


def _public(row):
    result = dict(row)
    if not result.get("pay_url") and result.get("status") in ("CREATING", "CREATE_UNKNOWN", "PENDING"):
        result["payment_notice"] = "原订单正在核实；支付链接可能遗失，请联系管理员，勿重复下单"
    return result


class Payments:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.entitlements = Entitlements(db, settings)
        self._tick_lock = asyncio.Lock()

    def _admin(self, admin_id):
        if admin_id not in self.settings.admin_ids:
            raise PaymentError("仅管理员可执行此操作")

    async def _api(self, method, params, credential, token=None):
        if method not in _METHODS:
            raise PaymentError("不支持的支付操作")
        token = token if token is not None else _decrypt(credential)
        form = signed_request(params, credential["merchant_id"], token)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), verify=True,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.post(API_ROOT + method, data=form)
        except (httpx.HTTPError, OSError):
            raise _Unavailable("支付平台连接未确认，请稍后查单") from None
        if response.status_code != 200 or len(response.content) > 65536:
            raise _Unavailable("支付平台暂不可用，请稍后查单")
        try:
            # Preserve number lexemes used by the provider's signature, including trailing zeros.
            body = json.loads(response.content, parse_float=str, parse_constant=_bad_constant,
                              object_pairs_hook=_json_object)
        except (ValueError, UnicodeError, RecursionError):
            raise _Untrusted("支付响应格式异常，请联系管理员") from None
        if not isinstance(body, dict):
            raise _Untrusted("支付响应格式异常，请联系管理员")
        if body.get("status") != "success":
            # Unsigned warnings, including 'not found', are never evidence of payment or failure.
            raise _Unavailable("支付平台暂未确认此请求，请稍后查单")
        if not verify(body, token):
            raise _Untrusted("支付响应验签失败，请联系管理员")
        if (_merchant(body.get("id")) != credential["merchant_id"]
                or isinstance(body.get("code"), bool) or body.get("code") not in (200, "200")
                or not isinstance(body.get("data"), dict)):
            raise _Untrusted("支付响应商户或格式异常，请联系管理员")
        return body["data"]

    async def config_summary(self, admin_id):
        self._admin(admin_id)
        row = await self.db.fetchrow("SELECT id,merchant_id FROM payment_credentials WHERE active ORDER BY id DESC LIMIT 1")
        return {"configured": row is not None, "merchant_id": row["merchant_id"] if row else "",
                "masked_token": "********" if row else "", "credential_id": row["id"] if row else None}

    async def configure(self, admin_id, merchant_id: str, token: str):
        self._admin(admin_id)
        try:
            merchant_id = _merchant(merchant_id)
        except _Untrusted:
            raise PaymentError("商户编号格式不正确") from None
        if merchant_id == "0" or not isinstance(token, str) or not 16 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise PaymentError("商户编号或支付密钥格式不正确")
        encrypted = _fernet().encrypt(token.encode("utf-8")).decode("ascii")
        try:
            await self._api("balance", {}, {"merchant_id": merchant_id}, token)
        except PaymentError:
            raise PaymentError("支付配置验证失败；原配置未变更，请检查凭据、网络或平台白名单") from None
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock($1::bigint)", _CONFIG_LOCK)
                await con.execute("UPDATE payment_credentials SET active=false WHERE active")
                await con.execute("INSERT INTO payment_credentials(merchant_id,token_encrypted,active,created_by) VALUES($1,$2,true,$3)",
                                  merchant_id, encrypted, admin_id)
                await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,details) VALUES($1,'PAYMENT_CONFIG','billing',$2)",
                                  admin_id, {"credential_rotated": True})
        result = await self.config_summary(admin_id)
        result["warning"] = "旧凭据保留用于历史订单；若平台禁用或轮换旧密钥，历史查单可能不可用"
        return result

    async def create_order(self, owner_id, product_code, request_key: str):
        product = PRODUCTS.get(product_code)
        if product is None:
            raise PaymentError("无效套餐")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 200 or any(ord(c) < 32 for c in request_key):
            raise PaymentError("购买确认已失效，请重新确认")
        async with self.db.pool.acquire() as con:
            lock = None
            try:
                async with con.transaction():
                    await con.execute("SELECT pg_advisory_xact_lock($1::bigint)", owner_id)
                    user = await con.fetchrow("SELECT status FROM users WHERE id=$1", owner_id)
                    if user is None or user["status"] != "ACTIVE":
                        raise PaymentError("用户不可购买，请联系管理员")
                    existing = await con.fetchrow("SELECT * FROM billing_orders WHERE request_key=$1", request_key)
                    if existing:
                        if existing["owner_id"] != owner_id or existing["product_code"] != product_code:
                            raise PaymentError("购买确认与原订单不匹配")
                        return _public(existing)
                    if product["extra"]:
                        access = await self.entitlements.snapshot(owner_id, con=con)
                        if (not access["active"] or access["exempt"] or access["plan_code"] != "week"
                                or access["expires_at"] is None or access["expires_at"] <= datetime.now(timezone.utc)):
                            raise PaymentError("加量包仅限当前有效周套餐购买")
                    credential = await con.fetchrow("SELECT * FROM payment_credentials WHERE active ORDER BY id DESC LIMIT 1")
                    if credential is None:
                        raise PaymentError("支付尚未配置，请联系管理员")
                    _decrypt(credential)
                    row = await con.fetchrow(
                        "INSERT INTO billing_orders(owner_id,unique_id,product_code,amount,coin,status,credential_id,request_key,next_check_at) "
                        "VALUES($1,$2,$3,$4,'USDT','CREATING',$5,$6,now()+interval '30 seconds') RETURNING *",
                        owner_id, "AD-" + secrets.token_hex(20), product_code, Decimal(product["amount"]), credential["id"], request_key)
                    # Take the order lock before its insert becomes visible to checkers.
                    lock = _ORDER_LOCK_BASE + row["id"]
                    await con.execute("SELECT pg_advisory_lock($1::bigint)", lock)
                return await self._create_link(con, row, product, credential)
            except asyncpg.UniqueViolationError:
                raise PaymentError("购买确认与原订单不匹配") from None
            finally:
                if lock is not None:
                    await asyncio.shield(con.execute("SELECT pg_advisory_unlock($1::bigint)", lock))

    async def _create_link(self, con, row, product, credential):
        try:
            data = await self._api("payLink", {"amount": product["amount"], "coin": "USDT", "unique_id": row["unique_id"],
                                               "name": product["title"]}, credential)
            provider = _provider_id(data.get("order_id"))
            url = _pay_url(data.get("pay_url"))
            if url != _canonical_pay_url(provider):
                raise _Untrusted("支付链接与平台订单不匹配，请联系管理员")
            if _status(data) != 0:
                raise _Untrusted("新建支付订单状态异常，请联系管理员")
            try:
                await con.execute("UPDATE billing_orders SET provider_order_id=$2,pay_url=$3,status='PENDING',error='',updated_at=now() "
                                  "WHERE id=$1 AND status='CREATING'", row["id"], provider, url)
            except asyncpg.UniqueViolationError:
                raise _Untrusted("支付平台订单重复，请联系管理员") from None
        except asyncio.CancelledError:
            await asyncio.shield(self._defer(row["id"], "支付创建结果未确认，请查原单", status="CREATE_UNKNOWN", con=con))
            raise
        except _Untrusted as exc:
            await self._defer(row["id"], str(exc), status="REVIEW", con=con)
        except PaymentError:
            await self._defer(row["id"], "支付创建结果未确认，请查原单", status="CREATE_UNKNOWN", con=con)
        return _public(await con.fetchrow("SELECT * FROM billing_orders WHERE id=$1", row["id"]))

    async def _defer(self, order_id, message, status=None, con=None):
        await (con if con is not None else self.db).execute(
            "UPDATE billing_orders SET status=CASE WHEN $3::text IS NOT NULL THEN $3 "
            "WHEN status='CREATING' THEN 'CREATE_UNKNOWN' ELSE status END,error=$2,updated_at=now() "
            "WHERE id=$1 AND status NOT IN ('PAID','REVIEW','FAILED')", order_id, message, status)

    def _validate_order(self, data, row):
        state = _status(data)
        provider = _provider_id(data.get("order_id"))
        if data.get("unique_id") != row["unique_id"] or (row["provider_order_id"] and row["provider_order_id"] != provider):
            raise _Untrusted("支付订单标识不匹配，请联系管理员")
        if "type" in data and data["type"] != "deposit":
            raise _Untrusted("支付订单类型异常，请联系管理员")
        if (state == 1 or "amount" in data) and _amount(data.get("amount")) != row["amount"]:
            raise _Untrusted("支付金额不匹配，请联系管理员")
        if "coin" in data and data["coin"] != row["coin"]:
            raise _Untrusted("支付币种不匹配，请联系管理员")
        return state, provider

    async def check_order(self, order_id, owner_id=None, admin_id=None):
        if admin_id is not None:
            self._admin(admin_id)
        elif owner_id is None:
            raise PaymentError("请提供订单所属用户")
        return await self._check_order(order_id, owner_id=owner_id, admin=admin_id is not None)

    async def _check_order(self, order_id, owner_id=None, admin=False, due_only=False):
        if isinstance(order_id, bool) or not isinstance(order_id, int) or not 0 < order_id < (1 << 62):
            raise PaymentError("订单不存在")
        async with self.db.pool.acquire() as con:
            lock = _ORDER_LOCK_BASE + order_id
            await con.execute("SELECT pg_advisory_lock($1::bigint)", lock)
            try:
                row = await con.fetchrow("SELECT * FROM billing_orders WHERE id=$1", order_id)
                if row is None or (owner_id is not None and not admin and row["owner_id"] != owner_id):
                    raise PaymentError("订单不存在")
                link_review = (row["status"] == "REVIEW" and not row["pay_url"]
                               and row["error"] == "支付链接异常，请联系管理员")
                if row["status"] == "PAID" or (row["status"] in ("REVIEW", "FAILED") and not admin and not link_review):
                    return _public(row)
                if due_only and row["next_check_at"] > datetime.now(timezone.utc):
                    return _public(row)
                delay = min(900, 30 * (2 ** min(row["attempts"], 5)))
                await con.execute("UPDATE billing_orders SET attempts=attempts+1,next_check_at=now()+$2::int*interval '1 second',updated_at=now() WHERE id=$1",
                                  order_id, delay)
                credential = await con.fetchrow("SELECT * FROM payment_credentials WHERE id=$1", row["credential_id"])
                try:
                    if credential is None:
                        raise PaymentError("历史支付配置不可用，请联系管理员")
                    first = await self._api("checkDeposit", {"unique_id": row["unique_id"]}, credential)
                    first_state, provider = self._validate_order(first, row)
                    try:
                        await con.execute("UPDATE billing_orders SET provider_order_id=$2,updated_at=now() WHERE id=$1", order_id, provider)
                    except asyncpg.UniqueViolationError:
                        raise _Untrusted("支付平台订单重复，请联系管理员") from None
                    second = await self._api("checkTransferByTxid", {"txid": provider}, credential)
                    second_state, second_provider = self._validate_order(second, row)
                    if second_provider != provider:
                        raise _Untrusted("两次查单的平台订单不一致，请联系管理员")
                    if second_state == 1 and second.get("coin") != row["coin"]:
                        raise _Untrusted("支付币种缺失或不匹配，请联系管理员")
                    if first_state == second_state == 1:
                        try:
                            return await self._settle(con, row, provider)
                        except _Untrusted:
                            raise
                        except Exception:
                            raise _Unavailable("支付发放暂未完成，请稍后查单") from None
                    if first_state != second_state:
                        raise _Unavailable("支付平台状态尚未同步，请稍后查单")
                    recovered_url = None
                    if not row["pay_url"]:
                        # Reconstruct only the documented receipt link for this signed order.
                        # No payLink retry and no entitlement grant for an unpaid order.
                        recovered_url = _canonical_pay_url(provider)
                    await con.execute("UPDATE billing_orders SET status='PENDING',pay_url=coalesce(nullif(pay_url,''),$2),error='',updated_at=now() "
                                      "WHERE id=$1 AND status<>'PAID'", order_id, recovered_url)
                except _Untrusted as exc:
                    await con.execute("UPDATE billing_orders SET status='REVIEW',error=$2,updated_at=now() WHERE id=$1 AND status<>'PAID'", order_id, str(exc))
                except PaymentError:
                    message = (row["error"] if link_review else
                               "支付平台尚未确认，请稍后查单；历史凭据失效请联系管理员")
                    await con.execute("UPDATE billing_orders SET status=CASE WHEN status='CREATING' THEN 'CREATE_UNKNOWN' ELSE status END,"
                                      "error=$2,updated_at=now() WHERE id=$1 AND status<>'PAID'", order_id, message)
                return _public(await con.fetchrow("SELECT * FROM billing_orders WHERE id=$1", order_id))
            finally:
                await asyncio.shield(con.execute("SELECT pg_advisory_unlock($1::bigint)", lock))

    async def _settle(self, con, row, provider):
        async with con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock($1::bigint)", row["owner_id"])
            current = await con.fetchrow("SELECT * FROM billing_orders WHERE id=$1 FOR UPDATE", row["id"])
            if current["status"] == "PAID":
                return _public(current)
            if current["provider_order_id"] != provider:
                raise _Untrusted("支付平台订单不匹配，请联系管理员")
            result = await self.entitlements.grant_order(con, current)
            evidence = {"provider_order_id": provider, "unique_id": current["unique_id"],
                        "amount": str(current["amount"]), "coin": current["coin"], "status": 1,
                        "checks": ["checkDeposit", "checkTransferByTxid"], "verified_at": datetime.now(timezone.utc).isoformat()}
            paid = await con.fetchrow("UPDATE billing_orders SET status='PAID',paid_at=now(),updated_at=now(),error='',evidence=$2,grant_result=$3 "
                                      "WHERE id=$1 RETURNING *", row["id"], evidence, result)
            return _public(paid)

    async def list_orders(self, owner_id, limit=8, offset=0):
        if isinstance(limit, bool) or isinstance(offset, bool) or not isinstance(limit, int) or not isinstance(offset, int) or offset < 0:
            raise PaymentError("无效分页参数")
        rows = await self.db.fetch("SELECT * FROM billing_orders WHERE owner_id=$1 ORDER BY id DESC LIMIT $2 OFFSET $3",
                                   owner_id, max(1, min(limit, 50)), offset)
        return [_public(row) for row in rows]

    async def tick(self):
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            # A fresh CREATING order may still be inside the original payLink request.
            rows = await self.db.fetch("SELECT id FROM billing_orders WHERE "
                                       "(status IN ('PENDING','CREATE_UNKNOWN') OR "
                                       "(status='REVIEW' AND (pay_url IS NULL OR pay_url='') AND error='支付链接异常，请联系管理员') OR "
                                       "(status='CREATING' AND created_at<=now()-interval '90 seconds')) "
                                       "AND next_check_at<=now() ORDER BY next_check_at,id LIMIT 8")
            semaphore = asyncio.Semaphore(2)

            async def check(row):
                async with semaphore:
                    try:
                        await self._check_order(row["id"], due_only=True)
                    except Exception:
                        # No exception/body logging here: credentials and provider data are sensitive.
                        pass

            await asyncio.gather(*(check(row) for row in rows))
