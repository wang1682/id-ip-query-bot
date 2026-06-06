#!/usr/bin/env python3
"""
身份证号 + IP归属地 查询 Telegram Bot
支持单条/批量查询，Excel附件上传

用法：
  1. 设置 BOT_TOKEN 环境变量
  2. python3 id_ip_bot.py

部署（作为 systemd service）：
  3. sudo ln -sf /root/id_ip_bot.py /usr/local/bin/id_ip_bot
     sudo tee /etc/systemd/system/id-ip-bot.service <<'SVC'
[Unit]
Description=ID & IP Query Telegram Bot
After=network.target

[Service]
Type=simple
User=root
ExecStart=/opt/hermes/venv/bin/python3 /root/id_ip_bot.py
Restart=always
RestartSec=10
EnvironmentFile=-/root/.hermes/.env

[Install]
WantedBy=multi-user.target
SVC
     sudo systemctl daemon-reload && sudo systemctl enable --now id-ip-bot
"""

import os
import re
import json
import io
import asyncio
import datetime
import tempfile
from pathlib import Path

import requests
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========== 配置 ==========
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("ID_IP_BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ 请设置 BOT_TOKEN 环境变量")
    print("   export BOT_TOKEN=你的bot_token")
    print("   或者放到 ~/.hermes/.env 里: ID_IP_BOT_TOKEN=...")
    exit(1)

# 加载身份证地区码表
CODE_TABLE_PATH = Path(__file__).parent / "id_area_code.json"
if CODE_TABLE_PATH.exists():
    with open(CODE_TABLE_PATH, encoding="utf-8") as f:
        AREA_CODE_MAP = json.load(f)
else:
    AREA_CODE_MAP = {}

# ========== 安全配置 ==========
# 白名单存储文件
WHITELIST_FILE = Path(__file__).parent / "whitelist.json"
# 管理员Telegram用户ID（数字ID，从 @userinfobot 获取）
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

# 频率限制（每分钟每个用户最多N次查询，0=不限）
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))
_rate_tracker = {}  # username -> [timestamp1, timestamp2, ...]


def load_whitelist() -> set:
    """从文件加载白名单"""
    if WHITELIST_FILE.exists():
        try:
            data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
            return set(data.get("users", []))
        except Exception:
            return set()
    return set()


def save_whitelist(users: set):
    """保存白名单到文件"""
    WHITELIST_FILE.write_text(
        json.dumps({"users": sorted(users)}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


ALLOWED_USERS = load_whitelist()


def check_rate_limit(username: str) -> bool:
    """检查是否超限，True=允许，False=拒绝"""
    if RATE_LIMIT <= 0:
        return True
    now = datetime.datetime.now().timestamp()
    if username not in _rate_tracker:
        _rate_tracker[username] = []
    # 清理60秒前的记录
    _rate_tracker[username] = [t for t in _rate_tracker[username] if now - t < 60]
    if len(_rate_tracker[username]) >= RATE_LIMIT:
        return False
    _rate_tracker[username].append(now)
    return True


def check_whitelist(user) -> bool:
    """检查用户是否在白名单中，True=放行"""
    if not ALLOWED_USERS:
        return True  # 不限制
    username = (user.username or "").strip().lower()
    if username in ALLOWED_USERS:
        return True
    # 也支持数字ID
    user_id = str(user.id)
    return user_id in ALLOWED_USERS


# IP查询间隔（秒）
IP_DELAY = 0.7
# ip-api.com 免费限制 ~45次/分钟

# 限制单次处理最大条数
MAX_BATCH = 500


# ========== 工具函数 ==========

def parse_id_num(id_str):
    """解析身份证号"""
    id_str = str(id_str).strip().upper()
    if not re.match(r'^\d{17}[\dX]$', id_str):
        return None

    # 校验码
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_chars = '10X98765432'
    total = sum(int(id_str[i]) * weights[i] for i in range(17))
    valid = (id_str[17] == check_chars[total % 11])

    code = id_str[:6]
    area = AREA_CODE_MAP.get(code, "未知地区")
    year, month, day = id_str[6:10], id_str[10:12], id_str[12:14]

    # 日期校验
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        valid = False

    gender = "男" if int(id_str[16]) % 2 else "女"
    status = "✅ 有效" if valid else "⚠️ 无效"
    birth = f"{year}-{month}-{day}"

    return {"area": area, "birth": birth, "gender": gender, "status": status}


def query_ip(ip):
    """查询IP归属地"""
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?lang=zh-CN"
            f"&fields=country,regionName,city,isp,org,mobile,proxy,hosting,query",
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("status") != "fail" and data.get("query"):
                loc = f"{data.get('country','')} {data.get('regionName','')} {data.get('city','')}".strip()
                isp = data.get("isp", "") or data.get("org", "")
                # 网络类型判定
                types = []
                if data.get("hosting"):
                    types.append("🖥 机房/托管")
                if data.get("proxy"):
                    types.append("🔒 代理/VPN")
                if data.get("mobile"):
                    types.append("📱 移动网络")
                if not types:
                    types.append("🏠 家庭宽带/普通")
                net_type = " | ".join(types)
                return loc, isp, net_type
        return None, None, None
    except Exception:
        return None, None, None


def extract_id_nums(text):
    """从文本中提取身份证号"""
    return re.findall(r'\b\d{17}[\dXx]\b', text.upper())


def extract_ips(text):
    """从文本中提取IP地址"""
    return re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)


def format_result_single(item_type, value, data):
    """格式化单条结果"""
    if item_type == "id":
        return (
            f"├ 📇 身份证\n"
            f"│  {value}\n"
            f"│  ├ 地区：{data['area']}\n"
            f"│  ├ 出生：{data['birth']}（{data['gender']}）\n"
            f"│  └ 状态：{data['status']}"
        )
    else:
        loc, isp, net_type = data
        line = f"├ 🌐 IP\n│  {value}\n│  ├ 归属地：{loc or '查询失败'}"
        if isp:
            line += f"\n│  ├ 运营商：{isp}"
        if net_type:
            line += f"\n│  └ 类型：{net_type}"
        return line


# ========== Bot Handlers ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **身份证 & IP 查询 Bot**\n\n"
        "发给我身份证号或IP地址，自动识别并查询。\n\n"
        "**使用方式：**\n"
        "📝 **文本** — 直接粘贴号码，支持混合多条\n"
        "  例如：\n"
        "  `110101199001011234`\n"
        "  `114.114.114.114`\n\n"
        "📎 **文件** — 上传 Excel(.xlsx/.xls) 或 CSV\n"
        "  自动识别「身份证号」「IP」列，批量查询\n"
        "  返回结果文件\n\n"
        "**限制：** 单次最多处理 500 条"
    )


# ========== 白名单管理命令 ==========

async def whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理白名单"""
    global ALLOWED_USERS
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    args = context.args
    if not args:
        if not ALLOWED_USERS:
            await update.message.reply_text("📋 白名单为空（所有人可用）")
        else:
            lines = [f"📋 白名单（共 {len(ALLOWED_USERS)} 人）:\n"]
            for u in sorted(ALLOWED_USERS):
                lines.append(f"  • @{u}")
            await update.message.reply_text("\n".join(lines))
        return

    cmd = args[0].lower()
    if cmd == "add" and len(args) >= 2:
        username = args[1].strip().lstrip("@").lower()
        if not username:
            await update.message.reply_text("❌ 用户名无效")
            return
        if username in ALLOWED_USERS:
            await update.message.reply_text(f"ℹ️ @{username} 已在白名单中")
            return
        ALLOWED_USERS.add(username)
        save_whitelist(ALLOWED_USERS)
        await update.message.reply_text(f"✅ @{username} 已加入白名单")

    elif cmd == "remove" and len(args) >= 2:
        username = args[1].strip().lstrip("@").lower()
        if not username:
            await update.message.reply_text("❌ 用户名无效")
            return
        if username not in ALLOWED_USERS:
            await update.message.reply_text(f"ℹ️ @{username} 不在白名单中")
            return
        ALLOWED_USERS.discard(username)
        save_whitelist(ALLOWED_USERS)
        await update.message.reply_text(f"🗑️ @{username} 已移出白名单")

    else:
        await update.message.reply_text(
            "用法：\n"
            "/whitelist — 查看白名单\n"
            "/whitelist add @xxx — 添加用户\n"
            "/whitelist remove @xxx — 移除用户"
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user = update.effective_user
    if not check_whitelist(user):
        return  # 非白名单，直接忽略
    
    username = (user.username or str(user.id)).lower()
    if not check_rate_limit(username):
        # 频率限制
        remaining_sec = 60 - (datetime.datetime.now().timestamp() - _rate_tracker[username][0])
        await update.message.reply_text(
            f"⏳ 查询过于频繁，请 {int(remaining_sec)} 秒后再试"
        )
        return
    
    text = update.message.text.strip()
    
    # 提取号码
    id_nums = extract_id_nums(text)
    ips = extract_ips(text)
    
    if not id_nums and not ips:
        await update.message.reply_text("⚠️ 未识别到身份证号或IP地址，请检查输入")
        return
    
    total = len(id_nums) + len(ips)
    if total > MAX_BATCH:
        await update.message.reply_text(f"❌ 超过单次限制（{MAX_BATCH}条），请分批发送")
        return
    
    await update.message.reply_text(f"🔍 正在查询 {total} 条，请稍候...")
    
    # 解析身份证
    results = []
    for num in id_nums:
        data = parse_id_num(num)
        if data:
            results.append(format_result_single("id", num, data))
    
    # 查询IP
    for i, ip in enumerate(ips):
        if i > 0:
            await asyncio.sleep(IP_DELAY)
        loc, isp, net_type = query_ip(ip)
        data = (loc or "查询失败", isp or "", net_type or "")
        results.append(format_result_single("ip", ip, data))
    
    # 拼接回复
    header = f"📊 查询结果（共 {len(results)} 条）\n\n"
    body = "\n\n".join(results)
    
    # Telegram 消息长度限制 ~4096
    if len(header + body) > 4000:
        # 太长就发文件
        content = header.replace("📊 ", "# ").replace("\n\n", "\n\n") + "\n".join(
            r.replace("├ ", "- ").replace("│", " ") 
            if r.startswith("├") or r.startswith("│") 
            else r 
            for r in results
        )
        await send_as_file(update, content, "查询结果.txt")
    else:
        await update.message.reply_text(header + body)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理上传的文件"""
    user = update.effective_user
    if not check_whitelist(user):
        return  # 非白名单，直接忽略
    
    username = (user.username or str(user.id)).lower()
    if not check_rate_limit(username):
        remaining_sec = 60 - (datetime.datetime.now().timestamp() - _rate_tracker[username][0])
        await update.message.reply_text(
            f"⏳ 操作过于频繁，请 {int(remaining_sec)} 秒后再试"
        )
        return
    
    file = update.message.document
    if not file:
        return
    
    # 检查格式
    fname = file.file_name or ""
    ext = os.path.splitext(fname)[1].lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        await update.message.reply_text("❌ 仅支持 .xlsx / .xls / .csv 格式")
        return
    
    await update.message.reply_text(f"📥 收到文件 {fname}，正在处理...")
    
    # 下载到临时文件
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        file_path = tmp.name
        tf = await file.get_file()
        await tf.download_to_drive(file_path)
    
    try:
        # 读取
        if ext == ".csv":
            df = pd.read_csv(file_path, dtype=str)
        else:
            df = pd.read_excel(file_path, dtype=str)
        
        df.columns = [c.strip() for c in df.columns]
        
        had_data = False
        total_ids, total_ips = 0, 0
        
        # ===== 处理身份证 =====
        id_col = next((c for c in df.columns if c in ("身份证号", "身份证", "ID卡号", "id_num", "id_no")), None)
        if id_col:
            results = df[id_col].apply(lambda x: parse_id_num(str(x).strip()) if pd.notna(x) else None)
            df["地区"] = [r["area"] if r else "" for r in results]
            df["出生日期"] = [r["birth"] if r else "" for r in results]
            df["性别"] = [r["gender"] if r else "" for r in results]
            df["状态"] = [r["status"] if r else "" for r in results]
            total_ids = len(df[id_col].dropna())
            had_data = True
        
        # ===== 处理IP =====
        ip_col = next((c for c in df.columns if c in ("IP", "IP地址", "ip", "ip_addr")), None)
        if ip_col:
            total_ips = len(df[ip_col].dropna())
            if total_ips > 200:
                await update.message.reply_text(
                    f"⚠️ IP地址共 {total_ips} 条（IP查询较慢，每次间隔{IP_DELAY}秒）\n"
                    f"预计需要约 {total_ips * IP_DELAY:.0f} 秒，请耐心等待..."
                )
            
            ip_results = []
            for idx, ip in enumerate(df[ip_col]):
                if pd.isna(ip):
                    ip_results.append(("", "", ""))
                    continue
                ip_str = str(ip).strip()
                if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
                    if idx > 0:
                        await asyncio.sleep(IP_DELAY)
                    loc, isp, net_type = query_ip(ip_str)
                    ip_results.append((loc or "查询失败", isp or "", net_type or ""))
                else:
                    ip_results.append(("格式错误", "", ""))
                
                if (idx + 1) % 50 == 0:
                    await update.message.reply_text(f"⏳ IP查询进度: {idx+1}/{total_ips}")
            
            df["IP归属地"] = [r[0] for r in ip_results]
            df["运营商"] = [r[1] for r in ip_results]
            df["网络类型"] = [r[2] for r in ip_results]
            had_data = True
        
        if not had_data:
            await update.message.reply_text("⚠️ 文件中未找到「身份证号」或「IP」列")
            os.unlink(file_path)
            return
        
        # 保存结果
        out_path = file_path.replace(ext, "_结果.xlsx")
        df.to_excel(out_path, index=False, engine="openpyxl")
        
        # 统计数据
        stats = []
        if total_ids:
            valid = sum(1 for r in df["状态"] if "有效" in str(r))
            stats.append(f"身份证 {total_ids} 条（有效 {valid}）")
        if total_ips:
            success = sum(1 for r in df["IP归属地"] if r and r != "查询失败" and r != "格式错误")
            stats.append(f"IP {total_ips} 条（成功 {success}）")
        
        with open(out_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"查询结果_{fname}",
                caption=f"✅ 查询完成\n" + "\n".join(stats)
            )
    
    except Exception as e:
        await update.message.reply_text(f"❌ 处理出错：{str(e)}")
    finally:
        try:
            os.unlink(file_path)
        except:
            pass


async def send_as_file(update, content, filename):
    """结果太长时以文件形式发送"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        tmp = f.name
    with open(tmp, "rb") as f:
        await update.message.reply_document(document=f, filename=filename)
    os.unlink(tmp)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ 出错了，请稍后重试")


# ========== 启动 ==========

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("whitelist", whitelist_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    
    app.add_error_handler(error_handler)
    
    print(f"🤖 Bot 已启动，Token: {BOT_TOKEN[:8]}...")
    app.run_polling()


if __name__ == "__main__":
    main()
