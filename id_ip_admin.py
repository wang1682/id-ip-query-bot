#!/usr/bin/env python3
"""
ID & IP Query Bot - Web Admin Panel
管理白名单、控制 Bot 启停、查看日志
"""
import json
import os
import subprocess
import datetime
import sqlite3
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ========== 路径 ==========
WHITELIST_FILE = Path("/root/whitelist.json")
LOG_DB = Path("/root/query_logs.db")
BOT_SERVICE = "id-ip-bot.service"
BOT_SCRIPT = "/root/id_ip_bot.py"

# ========== 静态页面 ==========
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IPIC Bot 管理面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
.header { background: #1e293b; padding: 20px 30px; border-bottom: 1px solid #334155; }
.header h1 { font-size: 20px; color: #38bdf8; }
.header span { color: #94a3b8; font-size: 13px; margin-left: 10px; }
.container { max-width: 900px; margin: 0 auto; padding: 24px; }
.card { background: #1e293b; border-radius: 10px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
.card h2 { font-size: 16px; margin-bottom: 15px; color: #94a3b8; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 500; transition: all .15s; }
.btn-primary { background: #2563eb; color: #fff; }
.btn-primary:hover { background: #1d4ed8; }
.btn-danger { background: #dc2626; color: #fff; }
.btn-danger:hover { background: #b91c1c; }
.btn-success { background: #16a34a; color: #fff; }
.btn-success:hover { background: #15803d; }
.btn-sm { padding: 5px 10px; font-size: 12px; }
.btn-outline { background: transparent; border: 1px solid #475569; color: #cbd5e1; }
.btn-outline:hover { background: #334155; }
input[type="text"] { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 8px 12px; border-radius: 6px; font-size: 13px; width: 200px; }
input[type="text"]:focus { outline: none; border-color: #2563eb; }
.flex { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mt-10 { margin-top: 10px; }
.mb-10 { margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; }
th { color: #64748b; font-weight: 500; font-size: 12px; text-transform: uppercase; }
tr:hover td { background: #0f172a; }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status-running { background: #22c55e; box-shadow: 0 0 6px #22c55e55; }
.status-stopped { background: #ef4444; box-shadow: 0 0 6px #ef444455; }
.log-box { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 12px; font-family: "SF Mono", monospace; font-size: 12px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; color: #94a3b8; }
.log-box .info { color: #22c55e; }
.log-box .err { color: #ef4444; }
.msg { padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-top: 10px; display: none; }
.msg-success { background: #166534; color: #bbf7d0; display: block; }
.msg-error { background: #7f1d1d; color: #fecaca; display: block; }
.empty { color: #64748b; font-size: 13px; padding: 20px 0; text-align: center; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #334155; color: #94a3b8; margin: 2px; }
</style>
</head>
<body>
<div class="header">
  <h1>🛠 IPIC Bot 管理 <span>@IPIC15467_bot</span></h1>
</div>
<div class="container">

  <!-- Bot状态 -->
  <div class="card">
    <div class="flex" style="justify-content: space-between;">
      <h2>🤖 Bot 状态</h2>
      <div class="flex">
        <button class="btn btn-sm btn-outline" onclick="botAction('restart')">重启</button>
        <button class="btn btn-sm btn-outline" id="toggleBtn" onclick="toggleBot()">停止</button>
      </div>
    </div>
    <div class="flex mt-10">
      <span id="statusText" style="font-size:15px;font-weight:600;">检查中...</span>
      <span id="uptimeText" style="color:#64748b;font-size:13px;"></span>
    </div>
  </div>

  <!-- 白名单 -->
  <div class="card">
    <h2>📋 白名单管理</h2>
    <div class="flex mb-10">
      <input type="text" id="newUser" placeholder="username 或 数字ID" onkeydown="if(event.key==='Enter')addUser()" style="flex:1;margin-right:8px;">
      <select id="newUserRole" style="margin-right:8px;padding:8px;border:1px solid #cbd5e1;border-radius:4px;">
        <option value="user">用户</option>
        <option value="admin">管理员</option>
      </select>
      <button class="btn btn-primary btn-sm" onclick="addUser()">+ 添加</button>
    </div>
    <div id="msgArea"></div>
    <table>
      <thead><tr><th style="width:30px;">#</th><th>用户</th><th>类型</th><th>角色</th><th style="width:80px;">操作</th></tr></thead>
      <tbody id="whitelistBody">
        <tr><td colspan="4" class="empty">加载中...</td></tr>
      </tbody>
    </table>
  </div>

  <!-- 查询日志 -->
  <div class="card">
    <div class="flex" style="justify-content: space-between;">
      <h2>🔍 查询记录 <span id="qlogSummary" style="color:#64748b;font-size:12px;"></span></h2>
      <div class="flex">
        <select id="qlogDays" onchange="loadQueryLogs()" style="background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:5px 8px;border-radius:6px;font-size:12px;">
          <option value="1">今天</option>
          <option value="3">3天</option>
          <option value="7" selected>7天</option>
          <option value="30">30天</option>
        </select>
        <button class="btn btn-sm btn-outline" onclick="loadQueryLogs()">刷新</button>
      </div>
    </div>
    <div id="queryLogBody" style="margin-top:10px;">
      <div class="empty">加载中...</div>
    </div>
  </div>

  <!-- 最近日志 -->
  <div class="card">
    <div class="flex" style="justify-content: space-between;">
      <h2>📄 最近日志</h2>
      <button class="btn btn-sm btn-outline" onclick="loadLogs()">刷新</button>
    </div>
    <div class="log-box" id="logBox">加载中...</div>
  </div>

</div>

<script>
// ======== 加载白名单 ========
async function loadWhitelist() {
  const r = await fetch('/api/whitelist');
  const d = await r.json();
  const tbody = document.getElementById('whitelistBody');
  if (!d.users || d.users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无白名单用户（所有人可用）</td></tr>';
    return;
  }
  const adminSet = new Set(d.admin || []);
  tbody.innerHTML = d.users.map((u, i) => {
    const type = /^\\d+$/.test(u) ? '数字ID' : '用户名';
    const role = adminSet.has(u) ? '⭐ 管理员' : '🔹 用户';
    return `<tr><td>${i+1}</td><td>${u}</td><td><span class="tag">${type}</span></td><td>${role}</td><td><button class="btn btn-sm btn-danger" onclick="removeUser('${u}')">移除</button></td></tr>`;
  }).join('');
}

async function addUser() {
  const input = document.getElementById('newUser');
  const role = document.getElementById('newUserRole').value;
  const val = input.value.trim();
  if (!val) return;
  const r = await fetch('/api/whitelist/add', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user: val, role}) });
  const d = await r.json();
  showMsg(d.ok ? '✅ '+d.msg : '❌ '+d.msg, d.ok);
  if (d.ok) { input.value = ''; loadWhitelist(); }
}

async function removeUser(user) {
  if (!confirm(`确定移除 ${user}？`)) return;
  const r = await fetch('/api/whitelist/remove', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({user}) });
  const d = await r.json();
  showMsg(d.ok ? '✅ '+d.msg : '❌ '+d.msg, d.ok);
  if (d.ok) loadWhitelist();
}

function showMsg(text, ok) {
  const el = document.getElementById('msgArea');
  el.innerHTML = `<div class="msg ${ok?'msg-success':'msg-error'}">${text}</div>`;
  setTimeout(() => el.innerHTML = '', 3000);
}

// ======== Bot 控制 ========
async function loadStatus() {
  const r = await fetch('/api/bot/status');
  const d = await r.json();
  const st = document.getElementById('statusText');
  const up = document.getElementById('uptimeText');
  const toggle = document.getElementById('toggleBtn');
  if (d.active) {
    st.innerHTML = '<span class="status-dot status-running"></span> 运行中';
    up.textContent = d.uptime || '';
    toggle.textContent = '停止';
    toggle.className = 'btn btn-sm btn-danger';
  } else {
    st.innerHTML = '<span class="status-dot status-stopped"></span> 已停止';
    up.textContent = '';
    toggle.textContent = '启动';
    toggle.className = 'btn btn-sm btn-success';
  }
}

async function toggleBot() {
  const r = await fetch('/api/bot/status');
  const d = await r.json();
  const action = d.active ? 'stop' : 'start';
  await fetch('/api/bot/'+action, {method:'POST'});
  setTimeout(loadStatus, 2000);
}

async function botAction(action) {
  await fetch('/api/bot/'+action, {method:'POST'});
  setTimeout(loadStatus, 2000);
}

// ======== 日志 ========
async function loadLogs() {
  document.getElementById('logBox').textContent = '加载中...';
  const r = await fetch('/api/logs');
  const d = await r.json();
  document.getElementById('logBox').textContent = d.logs || '(暂无日志)';
}

// ======== 查询日志 ========
async function loadQueryLogs() {
  const days = document.getElementById('qlogDays').value;
  const r = await fetch(`/api/query-logs?days=${days}`);
  const d = await r.json();
  const body = document.getElementById('queryLogBody');

  if (!d.ok || !d.logs || d.logs.length === 0) {
    body.innerHTML = '<div class="empty">暂无查询记录</div>';
    document.getElementById('qlogSummary').textContent = '';
    return;
  }

  // 汇总
  const types = { id: '身份证', ip: 'IP', batch_id: '批量身份证', batch_ip: '批量IP' };
  const parts = Object.entries(d.summary || {}).map(([k, v]) => `${types[k]||k}: ${v}次`);
  document.getElementById('qlogSummary').textContent = `（${parts.join(' | ')}）`;

  let html = `<table><thead><tr><th>时间</th><th>用户</th><th>类型</th><th>输入</th><th>条数</th></tr></thead><tbody>`;
  for (const log of d.logs) {
    const typeMap = { id: '🆔', ip: '🌐', batch_id: '📄🆔', batch_ip: '📄🌐' };
    const label = typeMap[log.type] || log.type;
    html += `<tr>
      <td style="white-space:nowrap;font-size:11px;color:#94a3b8;">${log.time}</td>
      <td>${log.username || log.user_id}</td>
      <td>${label}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${log.input}">${log.input}</td>
      <td>${log.count}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  body.innerHTML = html;
}

// ======== 初始化 ========
loadWhitelist();
loadStatus();
loadLogs();
loadQueryLogs();
setInterval(loadStatus, 30000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

# ========== API: 白名单 ==========

@app.route("/api/whitelist")
def api_whitelist():
    raw = load_whitelist_raw()
    return jsonify({
        "ok": True,
        "users": sorted(set(raw["admin"]) | set(raw["user"])),
        "admin": sorted(raw["admin"]),
        "user": sorted(raw["user"])
    })

@app.route("/api/whitelist/add", methods=["POST"])
def api_whitelist_add():
    data = request.get_json()
    if not data or "user" not in data:
        return jsonify({"ok": False, "msg": "缺少 user 参数"})
    user = data["user"].strip().lstrip("@").lower()
    role = data.get("role", "user").strip().lower()
    if role not in ("admin", "user"):
        role = "user"
    if not user:
        return jsonify({"ok": False, "msg": "用户名无效"})
    raw = load_whitelist_raw()
    all_users = set(raw["admin"]) | set(raw["user"])
    if user in all_users:
        return jsonify({"ok": False, "msg": f"@{user} 已在白名单"})
    raw[role].append(user)
    # 去重
    raw[role] = sorted(set(raw[role]))
    WHITELIST_FILE.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2)
    )
    _reload_bot()
    return jsonify({"ok": True, "msg": f"已添加 {user}（角色: {role}）"})

@app.route("/api/whitelist/remove", methods=["POST"])
def api_whitelist_remove():
    data = request.get_json()
    if not data or "user" not in data:
        return jsonify({"ok": False, "msg": "缺少 user 参数"})
    user = data["user"].strip().lstrip("@").lower()
    wl = load_whitelist()
    if user not in wl:
        return jsonify({"ok": False, "msg": f"{user} 不在白名单"})
    wl.discard(user)
    save_whitelist(wl)
    _reload_bot()
    return jsonify({"ok": True, "msg": f"已移除 {user}"})

# ========== API: Bot 控制 ==========

@app.route("/api/bot/status")
def api_bot_status():
    try:
        r = subprocess.run(["systemctl", "is-active", BOT_SERVICE],
                          capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
        uptime = ""
        if active:
            r2 = subprocess.run(["systemctl", "show", BOT_SERVICE, "--property=ActiveEnterTimestamp"],
                              capture_output=True, text=True, timeout=5)
            # format: ActiveEnterTimestamp=Sat 2026-06-06 20:42:35 UTC
            if "=" in r2.stdout:
                ts = r2.stdout.split("=", 1)[1].strip()
                uptime = f"（启动时间: {ts}）"
        return jsonify({"active": active, "uptime": uptime})
    except Exception as e:
        return jsonify({"active": False, "error": str(e)})

@app.route("/api/bot/restart", methods=["POST"])
def api_bot_restart():
    _run_systemctl("restart")
    return jsonify({"ok": True, "msg": "Bot 已重启"})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    _run_systemctl("stop")
    return jsonify({"ok": True, "msg": "Bot 已停止"})

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    _run_systemctl("start")
    return jsonify({"ok": True, "msg": "Bot 已启动"})

# ========== API: 日志 ==========

@app.route("/api/logs")
def api_logs():
    try:
        r = subprocess.run(
            ["journalctl", "-u", BOT_SERVICE, "-n", "50", "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=5
        )
        logs = r.stdout.strip() or "(暂无日志)"
        return jsonify({"logs": logs})
    except Exception as e:
        return jsonify({"logs": f"获取日志失败: {e}"})

# ========== API: 查询日志 ==========

@app.route("/api/query-logs")
def api_query_logs():
    """获取查询记录，支持筛选：?username=xxx&days=7"""
    try:
        if not LOG_DB.exists():
            return jsonify({"ok": True, "logs": []})
        
        username = request.args.get("username", "").strip().lower()
        days = request.args.get("days", "7")
        try:
            days = int(days)
        except ValueError:
            days = 7
        
        conn = sqlite3.connect(str(LOG_DB))
        conn.row_factory = sqlite3.Row

        # 计算时间范围
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        if username:
            rows = conn.execute(
                """SELECT id, user_id, username, query_type, query_input,
                          result_count, created_at
                   FROM query_logs
                   WHERE username LIKE ? AND created_at >= ?
                   ORDER BY id DESC LIMIT 200""",
                (f"%{username}%", cutoff)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, user_id, username, query_type, query_input,
                          result_count, created_at
                   FROM query_logs
                   WHERE created_at >= ?
                   ORDER BY id DESC LIMIT 200""",
                (cutoff,)
            ).fetchall()
        
        logs = []
        for r in rows:
            logs.append({
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"] or "",
                "type": r["query_type"],
                "input": (r["query_input"] or "")[:60],
                "count": r["result_count"],
                "time": r["created_at"]
            })
        
        # 统计数据
        stats = conn.execute(
            "SELECT query_type, COUNT(*) as cnt FROM query_logs WHERE created_at >= ? GROUP BY query_type",
            (cutoff,)
        ).fetchall()
        summary = {r["query_type"]: r["cnt"] for r in stats}
        
        conn.close()
        return jsonify({"ok": True, "logs": logs, "summary": summary})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "logs": []})

@app.route("/api/query-logs/stats")
def api_query_logs_stats():
    """按天统计查询量"""
    try:
        if not LOG_DB.exists():
            return jsonify({"ok": True, "stats": []})
        conn = sqlite3.connect(str(LOG_DB))
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """SELECT date(created_at) as day, query_type, COUNT(*) as cnt
               FROM query_logs
               WHERE created_at >= ?
               GROUP BY day, query_type ORDER BY day DESC""",
            (cutoff,)
        ).fetchall()
        conn.close()
        stats = [{"day": r[0], "type": r[1], "count": r[2]} for r in rows]
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "stats": []})

# ========== 工具函数 ==========

def load_whitelist_raw():
    """加载完整白名单（带角色）"""
    default = {"admin": [], "user": []}
    if WHITELIST_FILE.exists():
        try:
            data = json.loads(WHITELIST_FILE.read_text())
            if "users" in data:
                return {"admin": [], "user": data["users"]}
            return {"admin": data.get("admin", []), "user": data.get("user", [])}
        except:
            return default
    return default

def load_whitelist():
    """返回所有白名单用户（平铺）"""
    raw = load_whitelist_raw()
    return set(raw["admin"]) | set(raw["user"])

def save_whitelist(users):
    """保存白名单（保留已有角色）"""
    raw = load_whitelist_raw()
    # 从当前数据重建角色映射
    current = set(raw["admin"]) | set(raw["user"])
    removed = current - users
    added = users - current
    # 移除的用户
    raw["admin"] = [u for u in raw["admin"] if u not in removed]
    raw["user"] = [u for u in raw["user"] if u not in removed]
    # 新增的用户默认 user 角色
    for u in added:
        raw["user"].append(u)
    WHITELIST_FILE.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2)
    )

def save_whitelist(users):
    WHITELIST_FILE.write_text(
        json.dumps({"users": sorted(users)}, ensure_ascii=False, indent=2)
    )

def _reload_bot():
    """修改白名单后让 bot 重新加载（用 SIGHUP 或重启）"""
    subprocess.run(["systemctl", "restart", BOT_SERVICE],
                   capture_output=True, timeout=10)

def _run_systemctl(action):
    subprocess.run(["systemctl", action, BOT_SERVICE],
                   capture_output=True, timeout=30)

if __name__ == "__main__":
    print("🌐 IPIC Bot Admin Panel starting on http://127.0.0.1:8768")
    app.run(host="127.0.0.1", port=8768, debug=False)
