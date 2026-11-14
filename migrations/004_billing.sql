CREATE TABLE membership_segments (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id),
 plan_code TEXT NOT NULL CHECK(plan_code IN ('week','month','quarter')),
 starts_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ NOT NULL,
 source_key TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(ends_at>starts_at)
);
CREATE INDEX ON membership_segments(owner_id,starts_at,ends_at);
CREATE TABLE channel_credits (
 owner_id BIGINT PRIMARY KEY REFERENCES users(id), extra_channels INT NOT NULL DEFAULT 0 CHECK(extra_channels>=0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE payment_credentials (
 id BIGSERIAL PRIMARY KEY, merchant_id TEXT NOT NULL, token_encrypted TEXT NOT NULL,
 active BOOLEAN NOT NULL DEFAULT false, created_by BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_active_payment_credential ON payment_credentials(active) WHERE active;
CREATE TABLE billing_orders (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id), unique_id TEXT NOT NULL UNIQUE,
 product_code TEXT NOT NULL CHECK(product_code IN ('week','month','quarter','extra10','extra50')),
 amount NUMERIC(18,6) NOT NULL CHECK(amount>0), coin TEXT NOT NULL DEFAULT 'USDT',
 status TEXT NOT NULL CHECK(status IN ('CREATING','PENDING','CREATE_UNKNOWN','PAID','REVIEW','FAILED')),
 provider_order_id TEXT, pay_url TEXT, credential_id BIGINT NOT NULL REFERENCES payment_credentials(id),
 request_key TEXT NOT NULL UNIQUE, evidence JSONB NOT NULL DEFAULT '{}', grant_result JSONB NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', attempts INT NOT NULL DEFAULT 0, next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), paid_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ON billing_orders(provider_order_id) WHERE provider_order_id IS NOT NULL;
CREATE INDEX ON billing_orders(owner_id,created_at DESC);
CREATE INDEX ON billing_orders(next_check_at) WHERE status IN ('PENDING','CREATE_UNKNOWN');
CREATE TABLE payment_grants (
 order_id BIGINT PRIMARY KEY REFERENCES billing_orders(id), owner_id BIGINT NOT NULL REFERENCES users(id),
 product_code TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO app_settings(key,value) VALUES ('billing_enabled','false'),('billing_terms_version','"2026-11-08"') ON CONFLICT DO NOTHING;
UPDATE app_settings SET value='"@sad1881"',updated_at=now() WHERE key='contact_admin' AND value='"@KunKun"'::jsonb;
