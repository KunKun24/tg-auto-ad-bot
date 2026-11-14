"""Offline OKPay regressions; SQL uses only random schemas in auto_ad_test.

Run as adbot. HTTP is intercepted by MockTransport, never a live payment API.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from urllib.parse import parse_qs
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from adbot.db import Database
from adbot.billing_ui import _pay_url as ui_pay_url
from adbot.payments import PRODUCTS, PaymentError, Payments, _pay_url, build_base, sign, signed_request, verify


TEST_DSN = "postgresql://adbot@/auto_ad_test?host=/var/run/postgresql"
OWNER, OTHER, ADMIN = 33001, 33002, 33003
TOKEN = "TESTtoken123456789abcdefghijABCD"
SHOP = "10001"


def response(data, token=TOKEN, merchant=SHOP, **overrides):
    body = {"status": "success", "code": 200, "id": merchant, "data": data}
    body.update(overrides)
    body["sign"] = sign(body, token)
    return body


@pytest.mark.parametrize("params,base,expected", [
    ({"id": 10001, "amount": "100.5", "coin": "USDT", "unique_id": "ORDER-20260628-001",
      "timestamp": 1782680000, "nonce": "a1b2c3d4e5"},
     "amount=100.5&coin=USDT&id=10001&nonce=a1b2c3d4e5&timestamp=1782680000&unique_id=ORDER-20260628-001",
     "7444ADFD8E4F4DA09D752DDF9345E0EE56DC25090FCFAF675DD042830E5E3F79"),
    ({"status": "success", "code": 200, "id": 10001,
      "data": {"order_id": "abc123def456", "unique_id": "ORDER-20260628-001", "pay_user_id": 123456789,
               "amount": "100.5", "coin": "USDT", "status": 1, "type": "deposit"}},
     "code=200&data.amount=100.5&data.coin=USDT&data.order_id=abc123def456&data.pay_user_id=123456789&data.status=1&data.type=deposit&data.unique_id=ORDER-20260628-001&id=10001&status=success",
     "64B09C8847849FA6921D8FFBDF8E406D4A8EA623E53970712350F61783403F7D"),
    ({"id": 7, "a": "0", "b": 0, "c": "", "d": None, "e": False, "f": "hello", "nest": {"x": "1", "y": "2"}},
     "a=0&b=0&e=false&f=hello&id=7&nest.x=1&nest.y=2",
     "8BC0AF979075038025DDD51B6F4A2E6CF3FF9B5B5371EB2268D303F89883E92A"),
])
def test_official_vectors(params, base, expected):
    assert build_base(params) == base
    assert sign(params, TOKEN) == expected
    assert verify({**params, "sign": expected.lower()}, TOKEN)
    assert not verify({**params, "sign": "wrong"}, TOKEN)


def test_sign_complete_body_and_request_randomness():
    payload = response({"amount": "10.00", "status": 0})
    assert verify(payload, TOKEN)
    payload["code"] = 201
    assert not verify(payload, TOKEN)
    first, second = signed_request({}, SHOP, TOKEN), signed_request({}, SHOP, TOKEN)
    assert first["nonce"] != second["nonce"] and len(first["nonce"]) == 16
    assert verify(first, TOKEN) and "token" not in first
    assert build_base({"value": "a&b=x + 中文"}) == "value=a&b=x + 中文"


@pytest_asyncio.fixture
async def db():
    assert "/auto_ad_test?" in TEST_DSN and "/auto_ad?" not in TEST_DSN
    control = await asyncpg.connect(TEST_DSN)
    schema = "payments_" + uuid4().hex
    database = Database(TEST_DSN)
    try:
        assert await control.fetchval("SELECT current_database()") == "auto_ad_test"
        assert await control.fetchval("SELECT current_user") == "adbot"
        await control.execute(f'CREATE SCHEMA "{schema}"')

        async def init(con):
            for typ in ("json", "jsonb"):
                await con.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

        database.pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=8,
                                                  server_settings={"search_path": schema}, init=init, command_timeout=10)
        await database.migrate()
        for uid in (OWNER, OTHER, ADMIN):
            await database.execute("INSERT INTO users(id) VALUES($1)", uid)
        yield database
    finally:
        await database.close()
        assert schema.startswith("payments_") and len(schema) == 41
        await control.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.close()


class FakeOKPay:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.orders = {}
        self.tokens = {SHOP: TOKEN}
        self.mutate = None
        self.fail = None
        self.wait = None
        self.assert_durable = False
        self.missing_unpaid = False

    async def handle(self, request):
        assert str(request.url).startswith("https://api.okaypay.me/shop/")
        method = request.url.path.rsplit("/", 1)[1]
        assert method in ("balance", "payLink", "checkDeposit", "checkTransferByTxid")
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        token = self.tokens[form["id"]]
        assert verify(form, token) and "token" not in form
        self.calls.append((method, form))
        if self.wait:
            await self.wait(method, form)
        if self.fail and self.fail[0] == method:
            raise self.fail[1]
        if method == "balance":
            data = {"usdt": "0.00", "trx": "0", "cny": "0"}
        elif method == "payLink":
            if self.assert_durable:
                state = await self.db.fetchval("SELECT status FROM billing_orders WHERE unique_id=$1", form["unique_id"])
                assert state == "CREATING"
            key = form["unique_id"]
            assert key not in self.orders, "payLink must never be repeated"
            provider = key[3:35]
            data = {"order_id": provider, "pay_url": "https://t.me/OkayPayBot?start=shop_deposit--" + provider, "status": 0}
            self.orders[key] = {"order_id": data["order_id"], "unique_id": key, "status": 0,
                                "amount": form["amount"], "coin": form["coin"], "pay_user_id": OTHER,
                                "type": "deposit"}
        else:
            if method == "checkDeposit":
                found = self.orders.get(form["unique_id"])
            else:
                found = next((row for row in self.orders.values() if row["order_id"] == form["txid"]), None)
            if found is None:
                return httpx.Response(200, json={"status": "warning", "msg": "not found " + TOKEN})
            data = dict(found)
            if method == "checkDeposit":
                data.pop("coin", None)
            if self.missing_unpaid and data["status"] == 0:
                data.pop("amount", None)
                data.pop("coin", None)
        body = response(data, token=token, merchant=form["id"])
        if self.mutate:
            body = self.mutate(method, body)
        return httpx.Response(200, json=body)


@pytest_asyncio.fixture
async def rig(db, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("BILLING_ENCRYPTION_KEY", key.decode())
    fake = FakeOKPay(db)
    client = httpx.AsyncClient
    options = []

    def intercepted(**kwargs):
        options.append(kwargs.copy())
        return client(transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr("adbot.payments.httpx.AsyncClient", intercepted)
    settings = NS(admin_ids=(ADMIN,))
    return NS(db=db, payments=Payments(db, settings), peer=Payments(db, settings), fake=fake, key=key, options=options)


async def configured(rig):
    await rig.payments.configure(ADMIN, SHOP, TOKEN)


async def order(rig, product="week", owner=OWNER, key=None):
    return await rig.payments.create_order(owner, product, key or uuid4().hex)


def paid(rig, row):
    rig.fake.orders[row["unique_id"]]["status"] = 1


@pytest.mark.asyncio
async def test_configuration_encrypted_masked_verified_before_rotation(rig):
    summary = await rig.payments.config_summary(ADMIN)
    assert not summary["configured"]
    with pytest.raises(PaymentError):
        await rig.payments.configure(OWNER, SHOP, TOKEN)
    await configured(rig)
    credential = await rig.db.fetchrow("SELECT * FROM payment_credentials")
    assert TOKEN not in credential["token_encrypted"]
    assert Fernet(rig.key).decrypt(credential["token_encrypted"].encode()).decode() == TOKEN
    assert TOKEN not in str(await rig.payments.config_summary(ADMIN))
    assert rig.fake.calls[0][0] == "balance"
    assert all(options["verify"] and not options["follow_redirects"] and not options["trust_env"] for options in rig.options)
    rig.fake.tokens["20002"] = "NEWtoken123456789abcdefghijABCD"
    rig.fake.mutate = lambda method, body: {**body, "sign": "0" * 64}
    with pytest.raises(PaymentError) as error:
        await rig.payments.configure(ADMIN, "20002", rig.fake.tokens["20002"])
    assert TOKEN not in str(error.value)
    assert await rig.db.fetchval("SELECT id FROM payment_credentials WHERE active") == credential["id"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_credentials") == 1
    assert TOKEN not in str(await rig.db.fetch("SELECT details FROM audit_logs"))


@pytest.mark.asyncio
async def test_configuration_missing_key_and_cancellation_preserve_old(rig, monkeypatch):
    await configured(rig)
    old = await rig.payments.config_summary(ADMIN)
    monkeypatch.delenv("BILLING_ENCRYPTION_KEY")
    with pytest.raises(PaymentError):
        await rig.payments.configure(ADMIN, SHOP, TOKEN)
    monkeypatch.setenv("BILLING_ENCRYPTION_KEY", rig.key.decode())
    rig.fake.fail = ("balance", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await rig.payments.configure(ADMIN, SHOP, TOKEN)
    assert (await rig.payments.config_summary(ADMIN))["credential_id"] == old["credential_id"]


@pytest.mark.asyncio
async def test_create_durable_concurrent_request_idempotency_and_ownership(rig):
    await configured(rig)
    rig.fake.assert_durable = True
    rows = await asyncio.gather(rig.payments.create_order(OWNER, "week", "same-request"),
                                rig.peer.create_order(OWNER, "week", "same-request"))
    assert rows[0]["id"] == rows[1]["id"]
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1
    assert await rig.db.fetchval("SELECT count(*) FROM billing_orders") == 1
    with pytest.raises(PaymentError):
        await rig.payments.create_order(OWNER, "month", "same-request")
    with pytest.raises(PaymentError):
        await rig.payments.create_order(OTHER, "week", "same-request")
    with pytest.raises(PaymentError):
        await rig.payments.check_order(rows[0]["id"], owner_id=OTHER)
    with pytest.raises(PaymentError):
        await rig.payments.check_order(rows[0]["id"])
    assert not await rig.payments.list_orders(OTHER)
    assert (await rig.payments.list_orders(OWNER))[0]["id"] == rows[0]["id"]


@pytest.mark.asyncio
async def test_create_ambiguous_recovers_by_original_unique_without_recreating(rig):
    await configured(rig)
    rig.fake.fail = ("payLink", httpx.ReadTimeout("secret " + TOKEN))
    row = await order(rig, key="ambiguous")
    assert row["status"] == "CREATE_UNKNOWN" and not row["pay_url"] and row["payment_notice"]
    assert TOKEN not in row["error"]
    rig.fake.fail = None
    # Simulate the server accepting the request before the connection was lost.
    rig.fake.orders[row["unique_id"]] = {"order_id": "accepted-lost-response", "unique_id": row["unique_id"],
                                         "amount": "10", "coin": "USDT", "status": 0}
    same = await rig.peer.create_order(OWNER, "week", "ambiguous")
    assert same["id"] == row["id"]
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["provider_order_id"] == "accepted-lost-response" and recovered["status"] == "PENDING"
    assert recovered["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--accepted-lost-response"
    assert not recovered.get("payment_notice")
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    paid(rig, recovered)
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "PAID"
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
async def test_create_cancellation_is_ambiguous_and_never_retried(rig):
    await configured(rig)
    rig.fake.fail = ("payLink", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await order(rig, key="cancelled-create")
    row = await rig.db.fetchrow("SELECT * FROM billing_orders")
    assert row["status"] == "CREATE_UNKNOWN"
    assert (await order(rig, key="cancelled-create"))["id"] == row["id"]
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://t.me/okpay", "https://t.me.evil/okpay", "https://t.me/other", "https://t.me@evil/okpay", "tg://resolve?domain=other"])
async def test_create_rejects_signed_malicious_links(rig, url):
    await configured(rig)

    def mutate(method, body):
        if method == "payLink":
            body["data"]["pay_url"] = url
            body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    row = await order(rig)
    assert row["status"] == "REVIEW" and not row["pay_url"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_unpaid_incomplete_fields_allowed_but_no_grant(rig):
    await configured(rig)
    row = await order(rig)
    rig.fake.missing_unpaid = True
    current = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert current["status"] == "PENDING" and not current["paid_at"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert [method for method, _ in rig.fake.calls][-2:] == ["checkDeposit", "checkTransferByTxid"]


@pytest.mark.asyncio
async def test_paid_dual_checks_concurrent_grant_once_gift_allowed(rig):
    await configured(rig)
    row = await order(rig)
    paid(rig, row)
    rows = await asyncio.gather(rig.payments.check_order(row["id"], owner_id=OWNER),
                                rig.peer.check_order(row["id"], owner_id=OWNER))
    assert all(row["status"] == "PAID" for row in rows)
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 1
    segment = await rig.db.fetchrow("SELECT * FROM membership_segments")
    assert segment["owner_id"] == OWNER and segment["ends_at"] - segment["starts_at"] == timedelta(days=7)
    assert await rig.db.fetchval("SELECT count(*) FROM audit_logs WHERE action='PAYMENT_GRANT'") == 1
    assert Counter(method for method, _ in rig.fake.calls)["checkDeposit"] == 1
    assert "sign" not in rows[0]["evidence"] and "pay_user_id" not in rows[0]["evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,field,value,resign", [
    ("checkDeposit", "sign", "0" * 64, False),
    ("checkTransferByTxid", "sign", "0" * 64, False),
    ("checkDeposit", "id", "99999", True),
    ("checkTransferByTxid", "id", "99999", True),
    ("checkDeposit", "unique_id", "wrong", True),
    ("checkTransferByTxid", "unique_id", "wrong", True),
    ("checkDeposit", "amount", "9.999999999999999999999999", True),
    ("checkTransferByTxid", "amount", "10.00000000000000000001", True),
    ("checkTransferByTxid", "amount", None, True),
    ("checkTransferByTxid", "coin", None, True),
    ("checkTransferByTxid", "coin", "TRX", True),
    ("checkDeposit", "type", "withdraw", True),
    ("checkTransferByTxid", "type", "withdraw", True),
    ("checkTransferByTxid", "status", 2, True),
    ("checkTransferByTxid", "order_id", "wrong-provider", True),
    ("checkDeposit", "amount", "NaN", True),
    ("checkTransferByTxid", "amount", "Infinity", True),
])
async def test_invalid_paid_evidence_review_never_grants(rig, method, field, value, resign):
    await configured(rig)
    row = await order(rig)
    paid(rig, row)

    def mutate(current, body):
        if current == method:
            target = body if field in ("sign", "id") else body["data"]
            if value is None:
                target.pop(field, None)
            else:
                target[field] = value
            if resign:
                body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    current = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert current["status"] == "REVIEW" and not current["paid_at"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert await rig.db.fetchval("SELECT count(*) FROM membership_segments") == 0
    before = len(rig.fake.calls)
    await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert len(rig.fake.calls) == before
    rig.fake.mutate = None
    assert (await rig.payments.check_order(row["id"], admin_id=ADMIN))["status"] == "PAID"


@pytest.mark.asyncio
async def test_unsigned_warning_never_marks_failed_or_paid_and_masks_message(rig):
    await configured(rig)
    row = await order(rig)
    rig.fake.orders.clear()
    current = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert current["status"] == "PENDING" and TOKEN not in current["error"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_paid_transaction_rolls_back_grant_and_retries(rig, monkeypatch):
    await configured(rig)
    row = await order(rig)
    paid(rig, row)
    original = rig.payments.entitlements.grant_order

    async def broken(con, current):
        await original(con, current)
        raise RuntimeError("private " + TOKEN)

    monkeypatch.setattr(rig.payments.entitlements, "grant_order", broken)
    current = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert current["status"] == "PENDING" and not current["paid_at"]
    assert TOKEN not in current["error"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert await rig.db.fetchval("SELECT count(*) FROM membership_segments") == 0
    monkeypatch.setattr(rig.payments.entitlements, "grant_order", original)
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "PAID"


@pytest.mark.asyncio
async def test_old_order_uses_original_credential_after_rotation(rig):
    await configured(rig)
    row = await order(rig)
    rig.fake.tokens["20002"] = "NEWtoken123456789abcdefghijABCD"
    await rig.payments.configure(ADMIN, "20002", rig.fake.tokens["20002"])
    paid(rig, row)
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "PAID"
    assert all(form["id"] == SHOP for method, form in rig.fake.calls if method.startswith("check"))
    newer = await order(rig, product="month")
    assert row["credential_id"] != newer["credential_id"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_credentials") == 2


@pytest.mark.asyncio
async def test_addon_requires_current_non_admin_week_but_grants_after_expiry(rig):
    await configured(rig)
    for owner in (OWNER, ADMIN):
        with pytest.raises(PaymentError):
            await order(rig, product="extra10", owner=owner)
    row = await order(rig)
    paid(rig, row)
    await rig.payments.check_order(row["id"], owner_id=OWNER)
    addon = await order(rig, product="extra10")
    await rig.db.execute("UPDATE membership_segments SET starts_at=now()-interval '8 days',ends_at=now()-interval '1 day'")
    paid(rig, addon)
    assert (await rig.payments.check_order(addon["id"], owner_id=OWNER))["status"] == "PAID"
    assert await rig.db.fetchval("SELECT extra_channels FROM channel_credits WHERE owner_id=$1", OWNER) == 10
    assert await rig.db.fetchval("SELECT count(*) FROM membership_segments") == 1
    with pytest.raises(PaymentError):
        await order(rig, product="extra50")


@pytest.mark.asyncio
async def test_duplicate_provider_identity_never_grants_two_local_orders(rig):
    await configured(rig)
    first, second = await order(rig), await order(rig)
    rig.fake.orders[second["unique_id"]]["order_id"] = first["provider_order_id"]
    paid(rig, second)
    current = await rig.payments.check_order(second["id"], owner_id=OWNER)
    assert current["status"] == "REVIEW"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_tick_recovers_old_creating_only_due_statuses_max_two_and_backoff(rig):
    await configured(rig)
    rows = [await order(rig) for _ in range(6)]
    for row in rows:
        paid(rig, row)
    await rig.db.execute("UPDATE billing_orders SET next_check_at=now()-interval '1 second'")
    await rig.db.execute("UPDATE billing_orders SET status='REVIEW' WHERE id=$1", rows[0]["id"])
    await rig.db.execute("UPDATE billing_orders SET status='CREATING' WHERE id=$1", rows[1]["id"])
    await rig.db.execute("UPDATE billing_orders SET status='CREATING',created_at=now()-interval '91 seconds' WHERE id=$1", rows[2]["id"])
    active, maximum = 0, 0
    arrived = asyncio.Event()

    async def wait(method, _form):
        nonlocal active, maximum
        if method == "checkDeposit":
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                arrived.set()
            await arrived.wait()
            active -= 1

    rig.fake.wait = wait
    await asyncio.wait_for(rig.payments.tick(), timeout=5)
    assert maximum == 2
    statuses = dict(await rig.db.fetch("SELECT id,status FROM billing_orders"))
    assert statuses[rows[0]["id"]] == "REVIEW" and statuses[rows[1]["id"]] == "CREATING"
    assert all(statuses[row["id"]] == "PAID" for row in rows[2:])
    pending = await order(rig)
    await rig.db.execute("UPDATE billing_orders SET next_check_at=now()-interval '1 second',attempts=20 WHERE id=$1", pending["id"])
    rig.fake.wait = None
    await rig.payments.tick()
    current = await rig.db.fetchrow("SELECT attempts,next_check_at,updated_at FROM billing_orders WHERE id=$1", pending["id"])
    assert current["attempts"] == 21
    assert 899 <= (current["next_check_at"] - current["updated_at"]).total_seconds() <= 901
    count = len(rig.fake.calls)
    await rig.payments.tick()
    assert len(rig.fake.calls) == count


@pytest.mark.asyncio
async def test_network_has_no_row_transaction_lock(rig):
    await configured(rig)
    row = await order(rig)
    inspected = False

    async def wait(method, _form):
        nonlocal inspected
        if method == "checkDeposit":
            async with rig.db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute("SET LOCAL lock_timeout='500ms'")
                    assert await con.fetchval("SELECT id FROM billing_orders WHERE id=$1 FOR UPDATE", row["id"]) == row["id"]
                    assert await con.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", OWNER)
            inspected = True

    rig.fake.wait = wait
    await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert inspected


@pytest.mark.asyncio
async def test_transport_json_preserves_decimal_lexeme_and_rejects_duplicates(rig, monkeypatch):
    await configured(rig)
    row = await order(rig)
    paid(rig, row)
    original = rig.fake.handle

    async def numeric(request):
        result = await original(request)
        body = json.loads(result.content)
        if "check" in request.url.path:
            body["data"]["amount"] = "10.0000"
            body["sign"] = sign(body, TOKEN)
            raw = json.dumps(body).replace('"amount": "10.0000"', '"amount": 10.0000')
            return httpx.Response(200, content=raw)
        return result

    rig.fake.handle = numeric
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "PAID"
    second = await order(rig)

    async def duplicate(request):
        result = await original(request)
        if "check" in request.url.path:
            raw = result.content.decode().replace('"status":"success"', '"status":"success","status":"success"', 1)
            return httpx.Response(200, content=raw)
        return result

    rig.fake.handle = duplicate
    assert (await rig.payments.check_order(second["id"], owner_id=OWNER))["status"] == "REVIEW"


@pytest.mark.asyncio
async def test_rejects_dot_key_signature_shadowing(rig):
    await configured(rig)
    row = await order(rig)
    paid(rig, row)

    def shadow(method, body):
        if method == "checkDeposit":
            # The unchanged signature still verifies under the reference SDK.
            body["data"]["amount"] = "99"
            body["data.amount"] = "10"
            assert verify(body, TOKEN)
        return body

    rig.fake.mutate = shadow
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "REVIEW"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://t.me/okpay?start=shop_deposit----original123",
    "tg://resolve?domain=okpay&start=shop_deposit----original123",
    "https://t.me/OkayPayBot?start=shop_deposit--original123",
    "tg://resolve?domain=OkayPayBot&start=shop_deposit--original123",
])
async def test_documented_payment_urls_accepted(rig, url):
    await configured(rig)

    def mutate(method, body):
        if method == "payLink":
            unique = next(reversed(rig.fake.orders))
            rig.fake.orders[unique]["order_id"] = "original123"
            body["data"]["order_id"] = "original123"
            body["data"]["pay_url"] = url
            body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    row = await order(rig)
    assert row["status"] == "PENDING"
    assert row["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--original123"


@pytest.mark.asyncio
async def test_redirect_response_never_followed_or_paid(rig):
    await configured(rig)
    original = rig.fake.handle

    async def redirect(request):
        await original(request)
        return httpx.Response(302, headers={"location": "https://evil.example/?token=" + TOKEN})

    rig.fake.handle = redirect
    row = await order(rig)
    assert row["status"] == "CREATE_UNKNOWN" and not row["pay_url"]
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1
    assert TOKEN not in row["error"]


@pytest.mark.asyncio
async def test_user_product_and_addon_validation_before_paylink(rig):
    await configured(rig)
    with pytest.raises(PaymentError):
        await order(rig, product="unsupported")
    with pytest.raises(PaymentError):
        await rig.payments.create_order(OWNER, "week", "")
    await rig.db.execute("UPDATE users SET status='BANNED' WHERE id=$1", OWNER)
    with pytest.raises(PaymentError):
        await order(rig)
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 0


@pytest.mark.asyncio
async def test_immediate_check_waits_for_initial_link_and_create_holds_no_row_lock(rig):
    await configured(rig)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(method, form):
        if method == "payLink":
            async with rig.db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute("SET LOCAL lock_timeout='500ms'")
                    assert await con.fetchval("SELECT id FROM billing_orders WHERE unique_id=$1 FOR UPDATE", form["unique_id"])
                    assert await con.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", OWNER)
            entered.set()
            await release.wait()

    rig.fake.wait = wait
    creating = asyncio.create_task(order(rig))
    checking = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        order_id = await rig.db.fetchval("SELECT id FROM billing_orders")
        checking = asyncio.create_task(rig.peer.check_order(order_id, owner_id=OWNER))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(checking), timeout=0.05)
        assert Counter(method for method, _ in rig.fake.calls)["checkDeposit"] == 0
    finally:
        release.set()
        created = await creating
        checked = await checking if checking else None
    assert created["pay_url"] and checked["pay_url"] == created["pay_url"]
    assert checked["status"] == "PENDING"


@pytest.mark.parametrize("url", [
    "https://t.me/OkPay?start=shop_deposit----original123",
    "https://T.ME/oKpAy/?start=shop_deposit----original123",
    "tg://resolve?domain=OkPay&start=shop_deposit----original123",
    "https://t.me/OkayPayBot?start=shop_deposit--original123",
    "https://T.ME/oKaYpAyBoT/?start=shop_deposit--original123",
    "tg://resolve?domain=OkayPayBot&start=shop_deposit--original123",
])
def test_payment_url_casefold_is_shared_by_storage_and_ui(url):
    stored = _pay_url(url)
    assert stored == "https://t.me/OkayPayBot?start=shop_deposit--original123"
    assert ui_pay_url(url) == stored
    assert _pay_url(stored) == stored


@pytest.mark.parametrize("url", [
    "https://okpay@t.me/okpay?start=shop_deposit----x",
    "https://@t.me/okpay?start=shop_deposit----x",
    "tg://@resolve?domain=okpay&start=shop_deposit----x",
    "https://user:pass@t.me/okpay?start=shop_deposit----x",
    "https://t.me:443/okpay?start=shop_deposit----x",
    "https://t.me.evil.invalid/okpay?start=shop_deposit----x",
    "https://t.me./okpay?start=shop_deposit----x",
    "https://t.me\\@evil.invalid/okpay?start=shop_deposit----x",
    "https://t.me/okpay\\?start=shop_deposit----x",
    "https://t.me/okpay?start=shop_deposit----x#fragment",
    "https://t.me/okpay?start=shop_deposit----x\n",
    "https://t.me/okpay?start=shop_deposit----x\x7f",
    "https://t.me/okpay?start=first&start=second",
    "https://t.me/okpay?domain=okpay&domain=evil&start=x",
    "https://t.me/other?start=shop_deposit----x",
    "https://t.me/okpay/transfer?start=shop_deposit----x",
    "https://t.me/okpay//?start=shop_deposit----x",
    "https://t.me/%6fkpay?start=shop_deposit----x",
    "tg://user:pass@resolve?domain=okpay&start=x",
    "tg://resolve:443?domain=okpay&start=x",
    "tg://resolve?domain=okpay&domain=evil&start=x",
    "tg://resolve?domain=okpay&start=x&start=y",
    "tg://resolve/transfer?domain=okpay&start=x",
    "tg://resolve?domain=okpay.evil&start=x",
    "https://t.me/okpay",
    "https://t.me/okpay?start=",
    "https://t.me/okpay?start=original123",
    "https://t.me/okpay?start=shop_transfer----original123",
    "https://t.me/okpay?start=shop_withdraw----original123",
    "https://t.me/okpay?start=shop_deposit----",
    "https://t.me/okpay?start=shop_deposit----bad.provider",
    "https://t.me/okpay?start=shop_deposit----bad:provider",
    "https://t.me/okpay?start=shop_deposit----" + "X" * 49,
    "https://t.me/okpay?start=shop_deposit----original123&extra=unexpected",
    "tg://resolve?domain=okpay&start=shop_transfer----original123",
    "tg://resolve?domain=okpay&start=shop_deposit----original123&extra=unexpected",
    "https://t.me/okpay?start=shop_deposit--original123",
    "tg://resolve?domain=okpay&start=shop_deposit--original123",
    "https://t.me/OkayPayBot.evil?start=shop_deposit--original123",
    "https://t.me.evil.invalid/OkayPayBot?start=shop_deposit--original123",
    "https://t.me/OkayPayBot?start=shop_transfer--original123",
    "https://t.me/OkayPayBot?start=shop_deposit--original123&start=shop_deposit--other",
    "https://t.me/OkayPayBot?start=shop_deposit--bad.provider",
    "https://@t.me/OkayPayBot?start=shop_deposit--original123",
])
def test_payment_url_unsafe_forms_rejected_by_storage_and_ui(url):
    with pytest.raises(PaymentError):
        _pay_url(url)
    assert ui_pay_url(url) is None


async def seed_link_review(rig, *, provider="original123", error="支付链接异常，请联系管理员"):
    await configured(rig)
    row = await order(rig)
    rig.fake.orders[row["unique_id"]]["order_id"] = provider
    await rig.db.execute("UPDATE billing_orders SET status='REVIEW',error=$2,pay_url=NULL,provider_order_id=NULL,"
                         "next_check_at=now()-interval '1 second' WHERE id=$1", row["id"], error)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("via_tick", [False, True])
async def test_link_only_review_recovers_original_signed_unpaid_order_without_recreating_or_grant(rig, via_tick):
    row = await seed_link_review(rig)
    before = len(rig.fake.calls)
    if via_tick:
        await rig.payments.tick()
        recovered = await rig.db.fetchrow("SELECT * FROM billing_orders WHERE id=$1", row["id"])
    else:
        recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["status"] == "PENDING" and not recovered["paid_at"]
    assert recovered["id"] == row["id"] and recovered["unique_id"] == row["unique_id"]
    assert recovered["provider_order_id"] == "original123" and recovered["error"] == ""
    assert recovered["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--original123"
    assert ui_pay_url(recovered["pay_url"]) == recovered["pay_url"]
    assert [method for method, _ in rig.fake.calls[before:]] == ["checkDeposit", "checkTransferByTxid"]
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1
    assert await rig.db.fetchval("SELECT count(*) FROM billing_orders") == 1
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert await rig.db.fetchval("SELECT count(*) FROM membership_segments") == 0
    assert (await rig.peer.create_order(OWNER, "week", row["request_key"]))["id"] == row["id"]
    paid(rig, recovered)
    assert (await rig.payments.check_order(row["id"], owner_id=OWNER))["status"] == "PAID"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 1
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
async def test_link_review_owner_mismatch_never_queries_or_exposes_url(rig):
    row = await seed_link_review(rig)
    before = len(rig.fake.calls)
    with pytest.raises(PaymentError):
        await rig.payments.check_order(row["id"], owner_id=OTHER)
    assert len(rig.fake.calls) == before
    assert not (await rig.payments.list_orders(OTHER))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    "支付响应验签失败，请联系管理员", "支付金额不匹配，请联系管理员",
    "支付订单标识不匹配，请联系管理员", "支付链接异常，请联系管理员（附加文本）", "",
])
async def test_non_link_review_not_automatically_reopened_by_owner_or_tick(rig, error):
    row = await seed_link_review(rig, error=error)
    before = len(rig.fake.calls)
    current = await rig.payments.check_order(row["id"], owner_id=OWNER)
    await rig.payments.tick()
    stored = await rig.db.fetchrow("SELECT * FROM billing_orders WHERE id=$1", row["id"])
    assert current["status"] == stored["status"] == "REVIEW"
    assert stored["error"] == error and not stored["pay_url"]
    assert len(rig.fake.calls) == before
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method,field,value,resign", [
    ("checkDeposit", "sign", "0" * 64, False),
    ("checkDeposit", "id", "99999", True),
    ("checkDeposit", "unique_id", "wrong-local-order", True),
    ("checkDeposit", "amount", "11", True),
    ("checkDeposit", "type", "withdraw", True),
    ("checkTransferByTxid", "sign", "0" * 64, False),
    ("checkTransferByTxid", "unique_id", "wrong-local-order", True),
    ("checkTransferByTxid", "order_id", "different-provider", True),
    ("checkTransferByTxid", "amount", "11", True),
    ("checkTransferByTxid", "coin", "TRX", True),
])
async def test_link_recovery_revalidates_both_checks_before_publishing_url(rig, method, field, value, resign):
    row = await seed_link_review(rig)

    def mutate(current, body):
        if current == method:
            target = body if field in ("sign", "id") else body["data"]
            target[field] = value
            if resign:
                body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["status"] == "REVIEW" and not recovered["pay_url"]
    assert not recovered["paid_at"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bad:provider", "bad.provider", "X" * 49])
async def test_link_recovery_rejects_non_deeplink_provider_without_recreating(rig, provider):
    row = await seed_link_review(rig, provider=provider)
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert not recovered["pay_url"] and not recovered["paid_at"]
    assert recovered["status"] != "PAID"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [0, 1])
async def test_disagreeing_payment_statuses_never_restore_link_or_grant(rig, second_status):
    row = await seed_link_review(rig)
    rig.fake.orders[row["unique_id"]]["status"] = 1 - second_status

    def mutate(method, body):
        if method == "checkTransferByTxid":
            body["data"]["status"] = second_status
            body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert not recovered["pay_url"] and not recovered["paid_at"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_link_review_unsigned_warning_cannot_restore_or_grant(rig):
    row = await seed_link_review(rig)
    rig.fake.orders.clear()
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["status"] == "REVIEW" and not recovered["pay_url"]
    assert TOKEN not in recovered["error"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_created_link_provider_must_match_signed_order_provider(rig):
    await configured(rig)

    def mutate(method, body):
        if method == "payLink":
            body["data"]["pay_url"] = "https://t.me/OkPay?start=shop_deposit----another_order"
            body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    row = await order(rig)
    assert row["status"] == "REVIEW" and not row["pay_url"]
    assert row["error"] != "支付链接异常，请联系管理员"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1
    before = len(rig.fake.calls)
    rig.fake.mutate = None
    await rig.payments.check_order(row["id"], owner_id=OWNER)
    await rig.db.execute("UPDATE billing_orders SET next_check_at=now()-interval '1 second' WHERE id=$1", row["id"])
    await rig.payments.tick()
    assert len(rig.fake.calls) == before


@pytest.mark.asyncio
async def test_link_recovery_does_not_replace_existing_provider_binding(rig):
    row = await seed_link_review(rig)
    await rig.db.execute("UPDATE billing_orders SET provider_order_id='different-bound-provider' WHERE id=$1", row["id"])
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["status"] == "REVIEW" and not recovered["pay_url"]
    assert recovered["provider_order_id"] == "different-bound-provider"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["warning", "timeout", "unsynchronized"])
async def test_link_review_transient_failure_keeps_original_order_retryable(rig, failure):
    row = await seed_link_review(rig)
    if failure == "warning":
        provider_order = rig.fake.orders.pop(row["unique_id"])
    elif failure == "timeout":
        rig.fake.fail = ("checkDeposit", httpx.ReadTimeout("offline " + TOKEN))
    else:
        def unsynchronized(method, body):
            if method == "checkTransferByTxid":
                body["data"]["status"] = 1
                body["sign"] = sign(body, TOKEN)
            return body
        rig.fake.mutate = unsynchronized
    failed = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert failed["status"] == "REVIEW" and not failed["pay_url"] and not failed["paid_at"]
    if failure == "warning":
        rig.fake.orders[row["unique_id"]] = provider_order
    rig.fake.fail = rig.fake.mutate = None
    await rig.db.execute("UPDATE billing_orders SET next_check_at=now()-interval '1 second' WHERE id=$1", row["id"])
    await rig.payments.tick()
    recovered = await rig.db.fetchrow("SELECT * FROM billing_orders WHERE id=$1", row["id"])
    assert recovered["status"] == "PENDING"
    assert recovered["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--original123"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1


@pytest.mark.asyncio
async def test_real_signed_paylink_format_is_immediately_payable_without_recovery_tick(rig):
    await configured(rig)
    row = await order(rig)
    assert row["status"] == "PENDING" and row["error"] == "" and row["attempts"] == 0
    assert len(row["provider_order_id"]) == 32
    assert row["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--" + row["provider_order_id"]
    assert ui_pay_url(row["pay_url"]) == row["pay_url"]
    assert [method for method, _ in rig.fake.calls] == ["balance", "payLink"]
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert not row["paid_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://t.me/OkayPayBot?start=shop_deposit--another_order",
    "tg://resolve?domain=OkayPayBot&start=shop_deposit--another_order",
])
async def test_real_link_must_match_signed_provider_or_remain_review(rig, url):
    await configured(rig)

    def mutate(method, body):
        if method == "payLink":
            body["data"]["pay_url"] = url
            body["sign"] = sign(body, TOKEN)
        return body

    rig.fake.mutate = mutate
    row = await order(rig)
    assert row["status"] == "REVIEW" and not row["pay_url"]
    assert row["error"] == "支付链接与平台订单不匹配，请联系管理员"
    before = len(rig.fake.calls)
    rig.fake.mutate = None
    await rig.payments.check_order(row["id"], owner_id=OWNER)
    await rig.db.execute("UPDATE billing_orders SET next_check_at=now()-interval '1 second' WHERE id=$1", row["id"])
    await rig.payments.tick()
    assert len(rig.fake.calls) == before
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0


@pytest.mark.asyncio
async def test_link_review_empty_string_url_is_replaced_not_only_null(rig):
    row = await seed_link_review(rig)
    await rig.db.execute("UPDATE billing_orders SET pay_url='' WHERE id=$1", row["id"])
    recovered = await rig.payments.check_order(row["id"], owner_id=OWNER)
    assert recovered["status"] == "PENDING"
    assert recovered["pay_url"] == "https://t.me/OkayPayBot?start=shop_deposit--original123"
    assert await rig.db.fetchval("SELECT count(*) FROM payment_grants") == 0
    assert Counter(method for method, _ in rig.fake.calls)["payLink"] == 1
