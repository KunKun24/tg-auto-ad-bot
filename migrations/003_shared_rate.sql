CREATE TABLE api_rate_limits (
 bucket TEXT PRIMARY KEY,
 next_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO api_rate_limits(bucket) VALUES ('global');
