# IPIC Query Bot 🔍

身份证号 & IP 归属地查询 Telegram Bot — 支持单条文本、批量 Excel/CSV 文件上传。

## 功能

- **身份证号解析**：地区、出生日期、性别、校验码验证
- **IP 归属地查询**：国家/省份/城市、运营商、网络类型（家庭宽带/机房/代理/VPN/移动网络）
- **批量处理**：上传 Excel/CSV，自动识别列，返回结果文件
- **安全管理**：白名单（username + 数字ID）、频率限制（默认30次/分钟）、管理员命令

## 管理面板

Web 管理界面，支持运营人员操作：

- 📋 白名单管理（添加/移除用户）
- 🤖 Bot 启停控制（启动/停止/重启）
- 📄 实时日志查看

## 快速部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
export ID_IP_BOT_TOKEN="你的bot_token"
export ADMIN_ID="你的Telegram数字ID"

# 3. 启动 Bot
python3 id_ip_bot.py

# 4. 启动管理面板
python3 id_ip_admin.py
```

### systemd 部署

```bash
# 复制 service 文件
cp deploy/id-ip-bot.service /etc/systemd/system/
cp deploy/id-ip-admin.service /etc/systemd/system/

# 环境变量写入 /root/.hermes/.env
echo "ID_IP_BOT_TOKEN=你的token" >> /root/.hermes/.env
echo "ADMIN_ID=你的数字ID" >> /root/.hermes/.env

systemctl daemon-reload
systemctl enable --now id-ip-bot id-ip-admin
```

### Nginx 反代（管理面板）

```bash
cp deploy/nginx-bot-admin.conf /etc/nginx/sites-enabled/bot-admin
htpasswd -c /etc/nginx/.htpasswd admin
nginx -s reload
```

## 白名单管理

在 Telegram Bot 中：

```
/whitelist            — 查看白名单
/whitelist add @xxx   — 添加用户
/whitelist remove @xxx — 移除用户
```

或在 Web 管理面板中操作。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ID_IP_BOT_TOKEN` | Bot Token（必填） | — |
| `ADMIN_ID` | 管理员数字ID | — |
| `RATE_LIMIT` | 每分钟每用户查询上限 | 30 |
