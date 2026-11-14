# 安全策略

## 报告漏洞

如果你发现了安全漏洞，**不要开公开 issue**。请通过以下任一方式私下联系：

- Telegram：[@sad1881](https://t.me/sad1881)
- GitHub Security Advisories：仓库页 → Security → Report a vulnerability

请附上复现步骤、影响范围和你的环境（Python 版本、PostgreSQL 版本、是否自建部署）。我们会在确认后尽快修复，并在修复发布后于 release notes 中致谢（除非你希望匿名）。

## 仓库里不会有的东西

本仓库是公开版本，以下内容**全部不在仓库中**，也请勿提交：

- 真实 `BOT_TOKEN`、`ADMIN_IDS`、`DATABASE_URL`
- `BILLING_ENCRYPTION_KEY`（支付凭据的加密密钥）
- 生产数据库、`data/`、`logs/`、`backups/` 与任何实发回执
- 服务器地址、私有部署路径与运维凭据

仓库只提供 [.env.example](.env.example) 占位。如果你在提交里看到真实凭据，请立刻按下面的流程处理。

## 凭据泄露后的处理

1. 在 BotFather 里 `/revoke` 重新签发 Token，并重启两个服务。
2. 若 `BILLING_ENCRYPTION_KEY` 泄露：先处理完历史未完成订单，再用新密钥重新加密；**不要删除旧密钥**，否则历史订单将永久无法解密查单。
3. 若管理员 ID 或数据库凭据泄露：改 `ADMIN_IDS`、轮换数据库角色密码，并检查审计记录里的异常操作。
4. 用 `git filter-repo` 或 BFG 清理历史提交，强推后再视为泄露完成——只删最新一次提交无效。

## 部署侧建议

- `.env` 权限 `600`，属主为运行服务的系统账号；数据库只监听本机。
- 机器人不开放任何入站端口，不要额外加 Web 管理面板。
- 定期执行 `scripts/health.py`，并异地保存 `scripts/backup.py` 的产物与加密密钥。
- 支付凭据轮换时保留旧凭据用于历史查单，停用前确认没有待处理订单。

## 支付相关

项目接入第三方 USDT 渠道（OKPay），**不是 Telegram 官方 Stars 结算**。签名、金额、商户与订单绑定都会校验，但第三方渠道本身的风险、合规与到账争议由部署者承担。
