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

import sqlite3
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

# ========== 查询日志 SQLite ==========
LOG_DB = Path(__file__).parent / "query_logs.db"

def init_db():
    """初始化日志数据库"""
    conn = sqlite3.connect(str(LOG_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            username TEXT,
            query_type TEXT NOT NULL,
            query_input TEXT,
            result_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8:00'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            query_type TEXT NOT NULL,
            query_content TEXT,
            query_result TEXT,
            telegram_id TEXT NOT NULL,
            username TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT NOT NULL,
            username TEXT NOT NULL,
            login_at TEXT NOT NULL,
            expire_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def log_query(user_id, username, query_type, query_input, result_count=0):
    """记录一条查询日志"""
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(LOG_DB))
        conn.execute(
            "INSERT INTO query_logs (user_id, username, query_type, query_input, result_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(user_id), username, query_type, str(query_input)[:500], result_count, now)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 日志失败不影响主功能


# ========== 审计日志（新增） ==========
_AUDIT_COUNTER = 0

def generate_request_id():
    """生成流水号: QYYYYMMDDHHMMSSXXXXXX"""
    global _AUDIT_COUNTER
    _AUDIT_COUNTER += 1
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"Q{ts}{_AUDIT_COUNTER:06d}"

def write_audit_log(request_id, query_type, query_content, query_result, telegram_id, username):
    """写入审计日志"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        conn.execute(
            """INSERT INTO audit_logs 
               (request_id, query_type, query_content, query_result, telegram_id, username, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (request_id, query_type, str(query_content)[:500], str(query_result)[:1000],
             str(telegram_id), username, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 审计失败不影响主功能

def add_watermark(text, request_id, operator=""):
    """在结果底部添加审计水印"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{text}\n\n━━━━━━━━━━━━"]
    if operator:
        lines.append(f"操作者：{operator}")
    lines.append(f"查询时间：{ts}")
    lines.append(f"流水号：{request_id}")
    lines.append("━━━━━━━━━━━━")
    return "\n".join(lines)


# ========== 用户认证 ==========

SESSION_TIMEOUT = 30  # 分钟

def hash_pin(pin: str) -> str:
    """SHA256 哈希 PIN"""
    return hashlib.sha256(pin.encode()).hexdigest()

def get_session(telegram_id: str) -> dict | None:
    """获取当前有效会话"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT username, login_at, expire_at FROM user_sessions "
            "WHERE telegram_id = ? AND expire_at > ? ORDER BY id DESC LIMIT 1",
            (telegram_id, now)
        ).fetchone()
        conn.close()
        if row:
            return {"username": row[0], "login_at": row[1], "expire_at": row[2]}
        return None
    except Exception:
        return None

def require_session(telegram_id: str) -> str | None:
    """返回 session username 或 None（无有效会话）"""
    session = get_session(telegram_id)
    if not session:
        return None
    return session["username"]

def create_session(telegram_id: str, username: str):
    """创建 30 分钟会话"""
    try:
        now = datetime.datetime.now()
        expire = now + datetime.timedelta(minutes=SESSION_TIMEOUT)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        expire_str = expire.strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(LOG_DB))
        conn.execute(
            "INSERT INTO user_sessions (telegram_id, username, login_at, expire_at) VALUES (?, ?, ?, ?)",
            (telegram_id, username, now_str, expire_str)
        )
        conn.execute(
            "UPDATE bot_users SET last_login_at = ? WHERE username = ?",
            (now_str, username)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def add_bot_user(username: str, pin: str, role: str = "user") -> bool:
    """添加 bot 用户"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO bot_users (username, pin_hash, role, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (username, hash_pin(pin), role, now)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception:
        return False

def del_bot_user(username: str) -> bool:
    """删除 bot 用户"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        conn.execute("DELETE FROM bot_users WHERE username = ?", (username,))
        conn.execute("DELETE FROM user_sessions WHERE username = ?", (username,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def reset_bot_user_pin(username: str, new_pin: str) -> bool:
    """重置 PIN"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        conn.execute("UPDATE bot_users SET pin_hash = ? WHERE username = ?",
                     (hash_pin(new_pin), username))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def verify_login(username: str, pin: str) -> bool:
    """验证用户名和 PIN"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        row = conn.execute(
            "SELECT pin_hash, is_active FROM bot_users WHERE username = ?",
            (username,)
        ).fetchone()
        conn.close()
        if not row:
            return False
        if not row[1]:  # is_active = 0
            return False
        return row[0] == hash_pin(pin)
    except Exception:
        return False

def list_bot_users() -> list:
    """列出所有 bot 用户"""
    try:
        conn = sqlite3.connect(str(LOG_DB))
        rows = conn.execute(
            "SELECT username, role, is_active, created_at, last_login_at FROM bot_users ORDER BY id"
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


async def delete_message_later(bot, chat_id, message_id, delay, label=""):
    """延迟删除消息，删除失败只记录日志"""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"[auto-delete] {label} 删除失败 (chat={chat_id}, msg={message_id}): {e}", flush=True)


init_db()
# 白名单存储文件
WHITELIST_FILE = Path(__file__).parent / "whitelist.json"
# 管理员Telegram用户ID（数字ID，从 @userinfobot 获取）
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

# 频率限制（每分钟每个用户最多N次查询，0=不限）
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))
_rate_tracker = {}  # username -> [timestamp1, timestamp2, ...]

# ========== 消息自动销毁 ==========
AUTO_DELETE_ENABLED = True
AUTO_DELETE_USER_MESSAGE = True
AUTO_DELETE_USER_DELAY = 5

AUTO_DELETE_RESULT = True
AUTO_DELETE_RESULT_DELAY = 120

AUTO_DELETE_FILE = True
AUTO_DELETE_FILE_DELAY = 120


_WHITELIST_DATA = {}  # {"admin": set(), "user": set()}

def load_whitelist():
    """加载白名单，支持分级格式"""
    global _WHITELIST_DATA
    _WHITELIST_DATA = {"admin": set(), "user": set()}
    if WHITELIST_FILE.exists():
        try:
            data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
            if "users" in data:
                # 旧格式兼容: 管理员单独放 admin
                for u in data.get("users", []):
                    if u == os.getenv("ADMIN_ID", ""):
                        _WHITELIST_DATA["admin"].add(u)
                    else:
                        _WHITELIST_DATA["user"].add(u)
            else:
                _WHITELIST_DATA["admin"] = set(data.get("admin", []))
                _WHITELIST_DATA["user"] = set(data.get("user", []))
        except Exception:
            pass
    # 返回所有白名单用户（合并）
    return _WHITELIST_DATA["admin"] | _WHITELIST_DATA["user"]


def save_whitelist(users: set = None):
    """保存白名单到文件(保留角色信息)"""
    data = {
        "admin": sorted(_WHITELIST_DATA.get("admin", set())),
        "user": sorted(_WHITELIST_DATA.get("user", set())),
    }
    WHITELIST_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


ALLOWED_USERS = load_whitelist()


def check_rate_limit(username: str) -> bool:
    """check rate limit, True=allow, False=deny"""
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
    return _resolve_user(user) in ALLOWED_USERS

def _resolve_user(user) -> str:
    """返回用户的归一化标识（优先username，其次ID）"""
    return (user.username or "").strip().lower() or str(user.id)

def is_admin(user) -> bool:
    """检查用户是否为管理员"""
    uid = _resolve_user(user)
    return uid in _WHITELIST_DATA.get("admin", set()) or str(user.id) in _WHITELIST_DATA.get("admin", set())


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
    area = AREA_CODE_MAP.get(code, "")
    if not area:
        # 6位精确查找失败，退化到城市/省级
        city_code = code[:4]
        prov_code = code[:2]
        # 找前4位匹配的城市
        city_match = [v for k, v in AREA_CODE_MAP.items() if k.startswith(city_code)]
        if city_match:
            # "省 市 区" -> 取前两级
            parts = city_match[0].split()
            area = " ".join(parts[:2]) if len(parts) >= 2 else parts[0]
        else:
            # 退到省级
            prov_match = [v for k, v in AREA_CODE_MAP.items() if k.startswith(prov_code)]
            if prov_match:
                area = prov_match[0].split()[0]
            else:
                area = "未知地区"
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


def query_phone(phone_num):
    """查手机号归属地和运营商（离线 phone 库）"""
    try:
        from phone import Phone
        p = Phone()
        info = p.find(int(phone_num))
        if info and info.get("province"):
            area = f"{info.get('province', '')} {info.get('city', '')}".strip()
            carrier = info.get("phone_type", "")
            return area, carrier
        return None, None
    except Exception:
        return None, None


def extract_phone_nums(text):
    """从文本中提取11位手机号"""
    return re.findall(r'\b1[3-9]\d{9}\b', text)


def extract_bank_nums(text):
    """从文本中提取银行卡号（8-19位数字），排除手机号和身份证号"""
    all_nums = re.findall(r'\b\d{8,19}\b', text)
    phones = extract_phone_nums(text)
    idcards = extract_id_nums(text)
    # 过滤掉已经是手机号或身份证号的
    return [n for n in all_nums if n not in phones and n not in idcards]


def extract_name(text):
    """从文本中提取姓名（2-4汉字），过滤常见非姓名字词"""
    names = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
    exclude = {'查询', '结果', '手机', '电话', '号码', '身份证', '银行卡', '归属地', '运营商', 'IP', 'ip', '地址', '发送', '消息', '系统', '错误', '异常', '失败', '成功', '提示'}
    return [n for n in names if n not in exclude]


def format_result_single(item_type, value, data):
    """格式化单条结果（扁平格式，无树形符号）"""
    if item_type == "id":
        return (
            f"身份证号：{value}\n"
            f"地区：{data['area']}\n"
            f"出生：{data['birth']}（{data['gender']}）\n"
            f"状态：{data['status']}"
        )
    elif item_type == "phone":
        area, carrier = data
        line = f"手机号：{value}\n归属地：{area or '查询失败'}"
        if carrier:
            line += f"\n运营商：{carrier}"
        return line
    else:
        loc, isp, net_type = data
        line = f"IP：{value}\n归属地：{loc or '查询失败'}"
        if isp:
            line += f"\n运营商：{isp}"
        if net_type:
            line += f"\n类型：{net_type}"
        return line


# ========== 二要素验证 API（燕骏云 Yanjunyun） ==========
import hashlib, time

def verify_phone_name(mobile: str, name: str) -> dict:
    """运营商二要素验证（手机号+姓名）"""
    appid = os.getenv("YANJUN_APPID", "").strip()
    appsecret = os.getenv("YANJUN_APPSECRET", "").strip()
    apikey = os.getenv("YANJUN_APIKEY_VERIFY", "").strip()
    if not all([appid, appsecret, apikey]):
        return {"code": -1, "msg": "运营商二要素 API 未配置"}
    ts = int(time.time())
    sig = hashlib.md5(f"{appid}{ts}{appsecret}".encode()).hexdigest()
    try:
        r = requests.post("https://api.yanjunyun.com/operator/ispnametwocheck",
            data={"apikey": apikey, "timestamp": ts, "signature": sig, "name": name, "mobile": mobile},
            timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": f"运营商二要素请求失败: {e}"}

def verify_idcard(idcard: str, name: str) -> dict:
    """身份证二要素验证（身份证号+姓名）"""
    appid = os.getenv("YANJUN_APPID_IDCARD", "").strip()
    appsecret = os.getenv("YANJUN_APPSECRET_IDCARD", "").strip()
    apikey = os.getenv("YANJUN_APIKEY_IDCARD", "").strip()
    if not all([appid, appsecret, apikey]):
        return {"code": -1, "msg": "身份证二要素 API 未配置"}
    ts = int(time.time())
    sig = hashlib.md5(f"{appid}{ts}{appsecret}".encode()).hexdigest()
    try:
        r = requests.post("https://api.yanjunyun.com/identity/idcard",
            data={"apikey": apikey, "timestamp": ts, "signature": sig, "name": name, "idcard": idcard},
            timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": f"身份证二要素请求失败: {e}"}

def verify_bankcard(bankcard: str, name: str) -> dict:
    """银行卡二要素验证（银行卡号+姓名）"""
    appid = os.getenv("YANJUN_APPID", "").strip()
    appsecret = os.getenv("YANJUN_APPSECRET", "").strip()
    apikey = os.getenv("YANJUN_APIKEY_BANK", "").strip()
    if not all([appid, appsecret, apikey]):
        return {"code": -1, "msg": "银行卡二要素 API 未配置"}
    ts = int(time.time())
    sig = hashlib.md5(f"{appid}{ts}{appsecret}".encode()).hexdigest()
    try:
        r = requests.post("https://api.yanjunyun.com/bank/banktwodetailcheck",
            data={"apikey": apikey, "timestamp": ts, "signature": sig, "name": name, "bankcard": bankcard},
            timeout=10)
        return r.json()
    except Exception as e:
        return {"code": -1, "msg": f"银行卡二要素请求失败: {e}"}


def format_verify_result(vtype: str, value: str, name: str, api_result: dict) -> str:
    """格式化二要素验证结果（扁平格式）"""
    code = api_result.get("code", -1)
    if code != 200:
        return f"{vtype}二要素：❌ {api_result.get('msg', 'API异常')}"
    data = api_result.get("data", {})
    result_code = data.get("result")
    desc = data.get("desc", "")
    bankname = data.get("bankname", "")
    bankcardtype = data.get("bankcardtype", "")
    
    status_icon = "✅" if result_code == 1 else ("❌" if result_code == 2 else "⚠️")
    line = f"{vtype}二要素：{status_icon} {desc}"
    if bankname:
        line += f"（{bankname} {bankcardtype or ''}）".strip()
    return line


# ========== Bot Handlers ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 **身份证 & IP & 手机号 查询 Bot**\n\n"
        "发给我身份证号、IP地址或手机号，自动识别并查询。\n\n"
        "**使用方式：**\n"
        "📝 **文本** — 直接粘贴号码，支持混合多条\n"
        "  例如：\n"
        "  `110101199001011234`\n"
        "  `114.114.114.114`\n"
        "  `13800138000`\n"
        "  `/phone 13800138000`\n\n"
        "🔒 **自动二要素验证** — 1个号码+姓名（2-4汉字）\n"
        "  自动调 API 验证一致性\n"
        "  `张三 13800138000` → 归属地 + 运营商二要素\n"
        "  支持：手机号 / 身份证号 / 银行卡号\n\n"
        "📎 **文件** — 上传 Excel(.xlsx/.xls) 或 CSV\n"
        "  自动识别「身份证号」「IP」「手机号」列，批量查询\n"
        "  返回结果文件\n\n"
        "**登录：**\n"
        "  `/login 用户名 PIN` — 登录后可用查询功能\n"
        "  会话有效期 30 分钟\n\n"
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
        # 显示白名单（带角色）
        admin_u = sorted(_WHITELIST_DATA.get("admin", set()))
        user_u = sorted(_WHITELIST_DATA.get("user", set()))
        if not admin_u and not user_u:
            await update.message.reply_text("📋 白名单为空（所有人可用）")
        else:
            lines = [f"📋 白名单（管理员 {len(admin_u)} 人 用户 {len(user_u)} 人）:\n"]
            for u in user_u:
                tag = "🔹" if re.match(r'^\d+$', u) else "🔹"
                lines.append(f"  {tag} @{u}")
            for u in admin_u:
                tag = "⭐" if re.match(r'^\d+$', u) else "⭐"
                lines.append(f"  {tag} @{u}（管理员）")
            await update.message.reply_text("\n".join(lines))
        return

    cmd = args[0].lower()
    if cmd == "add" and len(args) >= 2:
        username = args[1].strip().lstrip("@").lower()
        role = "user"
        if len(args) >= 3:
            r = args[2].lower()
            if r in ("admin", "user"):
                role = r
        if not username:
            await update.message.reply_text("❌ 用户名无效")
            return
        if username in ALLOWED_USERS:
            await update.message.reply_text(f"ℹ️ @{username} 已在白名单中")
            return
        _WHITELIST_DATA[role].add(username)
        ALLOWED_USERS.add(username)
        save_whitelist()
        await update.message.reply_text(f"✅ @{username} 已加入白名单（角色: {role}）")
        # 通知管理员
        adder = f"@{user.username}" if user.username else f"ID: {user.id}"
        if ADMIN_ID:
            try:
                await context.application.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=f"📋 **白名单变更通知**\n\n➕ @{username} 已加入白名单\n角色: {role}\n操作人: {adder}"
                )
            except:
                pass

    elif cmd == "remove" and len(args) >= 2:
        username = args[1].strip().lstrip("@").lower()
        if not username:
            await update.message.reply_text("❌ 用户名无效")
            return
        if username not in ALLOWED_USERS:
            await update.message.reply_text(f"ℹ️ @{username} 不在白名单中")
            return
        ALLOWED_USERS.discard(username)
        _WHITELIST_DATA["admin"].discard(username)
        _WHITELIST_DATA["user"].discard(username)
        save_whitelist()
        await update.message.reply_text(f"🗑️ @{username} 已移出白名单")
        # 通知管理员
        remover = f"@{user.username}" if user.username else f"ID: {user.id}"
        if ADMIN_ID:
            try:
                await context.application.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=f"📋 **白名单变更通知**\n\n➖ @{username} 已移出白名单\n操作人: {remover}"
                )
            except:
                pass

    else:
        await update.message.reply_text(
            "用法：\\n"
            "/whitelist — 查看白名单\\n"
            "/whitelist add @xxx — 添加用户\\n"
            "/whitelist remove @xxx — 移除用户"
        )


# ========== 使用统计 ==========

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看今日查询统计（仅管理员）"""
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    try:
        conn = sqlite3.connect(str(LOG_DB))
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        # 今日总查询
        total = conn.execute(
            "SELECT COUNT(*) FROM query_logs WHERE created_at >= ?", (today,)
        ).fetchone()[0]
        # 按类型
        by_type = conn.execute(
            "SELECT query_type, COUNT(*) FROM query_logs WHERE created_at >= ? GROUP BY query_type ORDER BY COUNT(*) DESC",
            (today,)
        ).fetchall()
        # 活跃用户
        active = conn.execute(
            "SELECT username, COUNT(*) as cnt FROM query_logs WHERE created_at >= ? GROUP BY username ORDER BY cnt DESC LIMIT 10",
            (today,)
        ).fetchall()
        # 总记录数
        all_time = conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0]
        conn.close()

        lines = [f"📊 **今日统计（{today}）**\\n"]
        lines.append(f"今日查询: **{total}** 次")
        lines.append(f"累计查询: **{all_time}** 次\\n")
        if by_type:
            lines.append("**按类型：**")
            for t, c in by_type:
                emoji = {"id": "📇", "ip": "🌐", "batch_id": "📎📇", "batch_ip": "📎🌐"}.get(t, "📄")
                lines.append(f"  {emoji} {t}: {c}")
        if active:
            lines.append("\\n**活跃用户：**")
            for u, c in active:
                lines.append(f"  👤 @{u}: {c} 次")
        await update.message.reply_text("\\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ 获取统计失败: {e}")


# ========== 自动销毁开关 ==========

async def autodelete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看/设置自动销毁"""
    global AUTO_DELETE_ENABLED
    user = update.effective_user
    is_admin = ADMIN_ID and str(user.id) == ADMIN_ID
    
    args = context.args
    if args and args[0] in ("on", "off"):
        if not is_admin:
            await update.message.reply_text("❌ 只有管理员可以修改此设置")
            return
        AUTO_DELETE_ENABLED = (args[0] == "on")
        status = "✅ 开启" if AUTO_DELETE_ENABLED else "⏸️ 关闭"
        await update.message.reply_text(f"自动销毁已{status}")
        return
    
    status = "✅ 开启" if AUTO_DELETE_ENABLED else "⏸️ 关闭"
    lines = [
        f"📋 **自动销毁设置**\n",
        f"总开关：{status}",
        f"用户消息：{AUTO_DELETE_USER_DELAY}秒",
        f"查询结果：{AUTO_DELETE_RESULT_DELAY}秒",
        f"文件结果：{AUTO_DELETE_FILE_DELAY}秒",
    ]
    if AUTO_DELETE_FILE:
        lines.append(f"Excel处理中：{AUTO_DELETE_FILE_DELAY}秒")
    if is_admin:
        lines.append(f"\n管理员可用：`/autodelete on` / `off`")
    await update.message.reply_text("\n".join(lines))


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
    # 会话检查
    session_username = require_session(str(user.id))
    if not session_username:
        await update.message.reply_text("⚠️ 会话已过期，请重新登录：\n/login 用户名 PIN")
        return
    text = update.message.text.strip()

    # 提取号码
    id_nums = extract_id_nums(text)
    ips = extract_ips(text)
    phones = extract_phone_nums(text)
    bank_nums = extract_bank_nums(text)

    if not id_nums and not ips and not phones and not bank_nums:
        await update.message.reply_text("⚠️ 未识别到身份证号、IP地址或手机号，请检查输入")
        return

    total = len(id_nums) + len(ips) + len(phones) + len(bank_nums)
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

    # 查询手机号
    for i, p in enumerate(phones):
        if i > 0 or (i == 0 and id_nums):
            await asyncio.sleep(0.5)
        area, carrier = query_phone(p)
        data = (area or "查询失败", carrier or "")
        results.append(format_result_single("phone", p, data))

    # 查询IP
    for i, ip in enumerate(ips):
        if i > 0 or (i == 0 and (id_nums or phones)):
            await asyncio.sleep(IP_DELAY)
        loc, isp, net_type = query_ip(ip)
        data = (loc or "查询失败", isp or "", net_type or "")
        results.append(format_result_single("ip", ip, data))

    # ========== 自动二要素验证 ==========
    names = extract_name(text)
    if len(names) == 1:
        name = names[0]
        # 只有1条手机号+姓名 → 运营商二要素
        if len(phones) == 1 and not id_nums and not ips and not bank_nums:
            verify_result = verify_phone_name(phones[0], name)
            results.append(format_verify_result("运营商", phones[0], name, verify_result))
        # 只有1条身份证号+姓名 → 身份证二要素
        elif len(id_nums) == 1 and not phones and not ips and not bank_nums:
            verify_result = verify_idcard(id_nums[0], name)
            results.append(format_verify_result("身份证", id_nums[0], name, verify_result))
        # 只有1条银行卡号+姓名 → 银行卡二要素
        elif len(bank_nums) == 1 and not id_nums and not phones and not ips:
            verify_result = verify_bankcard(bank_nums[0], name)
            results.append(format_verify_result("银行卡", bank_nums[0], name, verify_result))

    # 生成流水号
    request_id = generate_request_id()
    
    # 记录日志
    # 写入审计日志（使用 session 用户名）
    for num in id_nums:
        write_audit_log(request_id, "id", num, "查询成功", user.id, session_username)
    for p in phones:
        write_audit_log(request_id, "phone", p, "查询成功", user.id, session_username)
    for ip in ips:
        write_audit_log(request_id, "ip", ip, "查询成功", user.id, session_username)
    for b in bank_nums:
        write_audit_log(request_id, "bank", b, "查询成功", user.id, session_username)
    
    # 记录日志（旧版）
    username_str = str(user.username or "") if user.username else str(user.id)
    if id_nums:
        log_query(user.id, username_str, "id", id_nums[0], len(id_nums))
    if phones:
        log_query(user.id, username_str, "phone", phones[0], len(phones))
    if ips:
        log_query(user.id, username_str, "ip", ips[0], len(ips))
    
    # 拼接回复
    header = f"📊 查询结果（共 {len(results)} 条）"
    
    # 如果有姓名，加在标题和结果之间
    name_line = ""
    if len(names) == 1:
        name_line = f"\n\n姓名：{names[0]}"
    
    body = "\n\n".join(results)
    full_reply = header + name_line + "\n\n" + body if results else header + name_line

    # Telegram 消息长度限制 ~4096
    if len(full_reply) > 4000:
        # 太长就发文件
        content_text = header + name_line + "\n\n" + "\n".join(
            r for r in results
        )
        content = add_watermark(content_text, request_id, session_username)
        sent = await send_as_file(update, content, "查询结果.txt")
    else:
        reply = add_watermark(full_reply, request_id, session_username)
        sent = await update.message.reply_text(reply)

    # 消息自动销毁
    if AUTO_DELETE_ENABLED and AUTO_DELETE_USER_MESSAGE and update.message:
        asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, update.message.message_id, AUTO_DELETE_USER_DELAY, label="用户消息"))
    if AUTO_DELETE_ENABLED and AUTO_DELETE_RESULT and sent:
        asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, sent.message_id, AUTO_DELETE_RESULT_DELAY, label="查询结果"))


async def phone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动查询手机号格式: /phone 13800138000"""
    user = update.effective_user
    if not check_whitelist(user):
        return

    username = (user.username or str(user.id)).lower()
    if not check_rate_limit(username):
        remaining_sec = 60 - (datetime.datetime.now().timestamp() - _rate_tracker[username][0])
        await update.message.reply_text(f"⏳ 查询过于频繁，请 {int(remaining_sec)} 秒后再试")
        return

    # 会话检查
    session_username = require_session(str(user.id))
    if not session_username:
        await update.message.reply_text("⚠️ 会话已过期，请重新登录：\n/login 用户名 PIN")
        return

    args = context.args
    if not args:
        await update.message.reply_text("用法：/phone 13800138000\\n支持多个号码用空格分隔")
        return

    phones = [p for p in args if re.match(r'^1[3-9]\d{9}$', p)]
    if not phones:
        await update.message.reply_text("❌ 未识别到有效手机号（11位大陆手机号）")
        return

    await update.message.reply_text(f"🔍 正在查询 {len(phones)} 个手机号...")
    results = []
    for i, p in enumerate(phones):
        if i > 0:
            await asyncio.sleep(0.5)
        area, carrier = query_phone(p)
        area_str = area or "查询失败"
        carrier_str = f"（{carrier}）" if carrier else ""
        results.append(f"{'├' if i+1 < len(phones) else '└'} {p} → {area_str}{carrier_str}")

    request_id = generate_request_id()
    for p in phones:
        write_audit_log(request_id, "phone_cmd", p, "查询成功", user.id, session_username)
    log_query(user.id, session_username, "phone_cmd", phones[0], len(phones))
    reply = f"📱 手机号查询结果（{len(phones)} 个）:\n" + "\n".join(results)
    reply = add_watermark(reply, request_id, session_username)
    sent = await update.message.reply_text(reply)

    # 消息自动销毁
    if AUTO_DELETE_ENABLED and AUTO_DELETE_USER_MESSAGE and update.message:
        asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, update.message.message_id, AUTO_DELETE_USER_DELAY, label="用户消息"))
    if AUTO_DELETE_ENABLED and AUTO_DELETE_RESULT and sent:
        asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, sent.message_id, AUTO_DELETE_RESULT_DELAY, label="查询结果"))


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
    # 会话检查
    session_username = require_session(str(user.id))
    if not session_username:
        await update.message.reply_text("⚠️ 会话已过期，请重新登录：\n/login 用户名 PIN")
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
    
    processing_msg = await update.message.reply_text(f"📥 收到文件 {fname}，正在处理...")
    
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
        total_ids, total_ips, total_phones = 0, 0, 0
        
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
                warn_msg = await update.message.reply_text(
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

        # ===== 处理手机号 =====
        phone_col = next((c for c in df.columns if c in ("手机号", "手机号码", "电话", "phone", "mobile")), None)
        if phone_col:
            total_phones = len(df[phone_col].dropna())
            phone_results = []
            for idx, p in enumerate(df[phone_col]):
                if pd.isna(p):
                    phone_results.append(("", ""))
                    continue
                phone_str = str(p).strip()
                if re.match(r'^1[3-9]\d{9}$', phone_str):
                    if idx > 0:
                        await asyncio.sleep(0.5)
                    area, carrier = query_phone(phone_str)
                    phone_results.append((area or "查询失败", carrier or ""))
                else:
                    phone_results.append(("格式错误", ""))
                if (idx + 1) % 50 == 0 and total_phones > 50:
                    await update.message.reply_text(f"⏳ 手机号查询进度: {idx+1}/{total_phones}")
            df["手机归属地"] = [r[0] for r in phone_results]
            df["运营商(手机)"] = [r[1] for r in phone_results]
            had_data = True

        if not had_data:
            await update.message.reply_text("⚠️ 文件中未找到「身份证号」「IP」或「手机号」列")
            os.unlink(file_path)
            return
        
        # 保存结果
        out_path = file_path.replace(ext, "_结果.xlsx")
        df.to_excel(out_path, index=False, engine="openpyxl")
        
        # 生成流水号 + 审计日志
        request_id = generate_request_id()
        stats = []
        if total_ids:
            valid = sum(1 for r in df["状态"] if "有效" in str(r))
            stats.append(f"身份证 {total_ids} 条（有效 {valid}）")
            write_audit_log(request_id, "batch_id", fname, f"身份证 {total_ids} 条（有效 {valid}）", user.id, session_username)
            log_query(user.id, session_username, "batch_id", fname, total_ids)
        if total_ips:
            success = sum(1 for r in df["IP归属地"] if r and r != "查询失败" and r != "格式错误")
            stats.append(f"IP {total_ips} 条（成功 {success}）")
            write_audit_log(request_id, "batch_ip", fname, f"IP {total_ips} 条（成功 {success}）", user.id, session_username)
            log_query(user.id, session_username, "batch_ip", fname, total_ips)
        if total_phones:
            success = sum(1 for r in df["手机归属地"] if r and r != "查询失败" and r != "格式错误")
            stats.append(f"手机号 {total_phones} 条（成功 {success}）")
            write_audit_log(request_id, "batch_phone", fname, f"手机号 {total_phones} 条（成功 {success}）", user.id, session_username)
            log_query(user.id, session_username, "batch_phone", fname, total_phones)
        
        caption = f"✅ 查询完成\n" + "\n".join(stats)
        caption = add_watermark(caption, request_id, session_username)
        with open(out_path, "rb") as f:
            sent = await update.message.reply_document(
                document=f,
                filename=f"查询结果_{fname}",
                caption=caption
            )

        # 消息自动销毁
        if AUTO_DELETE_ENABLED and AUTO_DELETE_FILE:
            msgs_to_delete = [
                (processing_msg, "处理中提示"),
            ]
            if total_ips > 200:
                try:
                    msgs_to_delete.append((warn_msg, "IP警告"))
                except NameError:
                    pass
            for msg_obj, label in msgs_to_delete:
                if msg_obj:
                    asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, msg_obj.message_id, AUTO_DELETE_FILE_DELAY, label=label))
            if sent:
                asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, sent.message_id, AUTO_DELETE_FILE_DELAY, label="结果文件"))
    
    except Exception as e:
        sent = await update.message.reply_text(f"❌ 处理出错：{str(e)}")
        if AUTO_DELETE_ENABLED and AUTO_DELETE_FILE and sent:
            asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, sent.message_id, AUTO_DELETE_FILE_DELAY, label="错误消息"))
    finally:
        try:
            os.unlink(file_path)
        except:
            pass
        # 用户文件消息：5秒后删除
        if AUTO_DELETE_ENABLED and AUTO_DELETE_USER_MESSAGE and update.message:
            asyncio.create_task(delete_message_later(context.bot, update.effective_chat.id, update.message.message_id, AUTO_DELETE_USER_DELAY, label="用户文件消息"))


async def send_as_file(update, content, filename):
    """结果太长时以文件形式发送"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        tmp = f.name
    with open(tmp, "rb") as f:
        sent = await update.message.reply_document(document=f, filename=filename)
    os.unlink(tmp)
    return sent


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}", flush=True)
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ 出错了，请稍后重试")


# ========== 用户认证命令 ==========

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户登录 /login 用户名 PIN"""
    user = update.effective_user
    if not check_whitelist(user):
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/login 用户名 PIN")
        return

    username_input, pin_input = args[0], " ".join(args[1:])
    if len(pin_input) > 64:
        await update.message.reply_text("❌ PIN 过长")
        return

    if verify_login(username_input, pin_input):
        create_session(str(user.id), username_input)
        await update.message.reply_text("✅ 登录成功，有效期 30 分钟")
        print(f"Login OK: user={username_input} telegram={user.id}", flush=True)
    else:
        print(f"Login FAIL: user={username_input} telegram={user.id}", flush=True)
        await update.message.reply_text("❌ 用户名或 PIN 错误")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员添加用户 /adduser 用户名 PIN [role]"""
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/adduser 用户名 PIN [role]")
        return

    username_input = args[0]
    pin_input = args[1]
    role = args[2] if len(args) > 2 else "user"

    if len(pin_input) > 64:
        await update.message.reply_text("❌ PIN 过长")
        return

    if add_bot_user(username_input, pin_input, role):
        await update.message.reply_text(f"✅ 用户 {username_input} 添加成功（{role}）")
        print(f"Admin adduser: {username_input} role={role}", flush=True)
    else:
        await update.message.reply_text(f"❌ 用户 {username_input} 已存在或添加失败")


async def deluser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员删除用户 /deluser 用户名"""
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    args = context.args
    if not args:
        await update.message.reply_text("用法：/deluser 用户名")
        return

    username_input = args[0]
    if del_bot_user(username_input):
        await update.message.reply_text(f"✅ 用户 {username_input} 已删除")
        print(f"Admin deluser: {username_input}", flush=True)
    else:
        await update.message.reply_text(f"❌ 删除失败，用户可能不存在")


async def resetpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员重置 PIN /resetpin 用户名 新PIN"""
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/resetpin 用户名 新PIN")
        return

    username_input = args[0]
    new_pin = " ".join(args[1:])
    if len(new_pin) > 64:
        await update.message.reply_text("❌ PIN 过长")
        return

    if reset_bot_user_pin(username_input, new_pin):
        await update.message.reply_text(f"✅ 用户 {username_input} PIN 已重置")
        print(f"Admin resetpin: {username_input}", flush=True)
    else:
        await update.message.reply_text(f"❌ 重置失败，用户可能不存在")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员列出用户 /users"""
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != ADMIN_ID:
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    rows = list_bot_users()
    if not rows:
        await update.message.reply_text("📋 暂无用户")
        return

    lines = ["📋 **用户列表**"]
    for r in rows:
        status = "✅" if r[2] else "❌"
        last_login = f" 最后登录: {r[4][:19]}" if r[4] else ""
        lines.append(f"\\n{status} {r[0]} ({r[1]}){last_login}")
    await update.message.reply_text("\\n".join(lines))


# ========== 启动 ==========

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("whitelist", whitelist_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("phone", phone_cmd))
    app.add_handler(CommandHandler("autodelete", autodelete_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CommandHandler("resetpin", resetpin_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    
    app.add_error_handler(error_handler)
    
    print(f"🤖 Bot 已启动，Token: {BOT_TOKEN[:8]}...", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
