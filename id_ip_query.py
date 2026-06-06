#!/usr/bin/env python3
"""
批量查询工具：身份证号解析 + IP地址归属地
支持 Excel (.xlsx / .xls) 和 CSV 格式

输入文件要求（二选一或同时）：
  - 包含"身份证号"列，自动解析地域+出生日期+性别
  - 包含"IP"列，自动查询IP归属地

输出：在原文件名后加 "_结果.xlsx"

用法：
  python3 id_ip_query.py 文件路径
  python3 id_ip_query.py 文件路径 --delay 1.5   # IP查询间隔(秒,默认1)
"""

import sys
import json
import time
import re
import os
import requests
import pandas as pd
from pathlib import Path

# ========== 加载身份证地区码表 ==========
SCRIPT_DIR = Path(__file__).parent
CODE_TABLE = SCRIPT_DIR / "id_area_code.json"

if CODE_TABLE.exists():
    with open(CODE_TABLE, encoding="utf-8") as f:
        AREA_CODE_MAP = json.load(f)
else:
    AREA_CODE_MAP = {}

def parse_id(id_num):
    """解析身份证号，返回 (地区, 出生日期, 性别, 是否合法)"""
    id_str = str(id_num).strip().upper()
    
    # 基本校验：18位数字或17位+X
    if not re.match(r'^\d{17}[\dX]$', id_str):
        return ("格式错误", "格式错误", "格式错误", False)
    
    # 校验码验证
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_chars = '10X98765432'
    total = sum(int(id_str[i]) * weights[i] for i in range(17))
    expected_check = check_chars[total % 11]
    valid = (id_str[17] == expected_check)
    
    # 地区码（前6位）
    code = id_str[:6]
    area = AREA_CODE_MAP.get(code, "未知")
    
    # 出生日期（7-14位）
    year, month, day = id_str[6:10], id_str[10:12], id_str[12:14]
    birth = f"{year}-{month}-{day}"
    
    # 检查日期合法性
    try:
        import datetime
        datetime.date(int(year), int(month), int(day))
    except:
        valid = False
        if not area or area == "未知":
            pass  # 双重不合法
    
    # 性别（17位）
    gender = "男" if int(id_str[16]) % 2 else "女"
    
    valid_str = "有效" if valid else "无效"
    return (area, birth, gender, valid_str)


def query_ip(ip, delay=1.0):
    """查询IP归属地，使用 ip-api.com 免费接口"""
    time.sleep(delay)  # 限速
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                isp = data.get("isp", "")
                org = data.get("org", "")
                provider = isp or org or ""
                return f"{data.get('country','')} {data.get('regionName','')} {data.get('city','')}", provider
            else:
                return "查询失败", ""
        else:
            return f"HTTP {r.status_code}", ""
    except Exception as e:
        return f"连接错误", ""


def process_file(filepath, ip_delay=1.0):
    """主处理函数"""
    fp = Path(filepath)
    if not fp.exists():
        print(f"❌ 文件不存在: {filepath}")
        return
    
    # 读取文件
    ext = fp.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(fp, dtype=str)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(fp, dtype=str)
    else:
        print(f"❌ 不支持的文件格式: {ext}，仅支持 .csv / .xlsx / .xls")
        return
    
    # 清理列名空格
    df.columns = [c.strip() for c in df.columns]
    
    print(f"📄 读取文件: {fp.name}")
    print(f"   行数: {len(df)}")
    print(f"   列名: {list(df.columns)}")
    print()
    
    # ============ 身份证号处理 ============
    id_col = None
    for col in ["身份证号", "身份证", "ID卡号", "id_num", "id_no"]:
        if col in df.columns:
            id_col = col
            break
    
    if id_col:
        print(f"🔍 检测到身份证号列: 「{id_col}」，开始解析...")
        results = df[id_col].apply(parse_id)
        df["身份证-地区"] = results.apply(lambda x: x[0])
        df["身份证-出生日期"] = results.apply(lambda x: x[1])
        df["身份证-性别"] = results.apply(lambda x: x[2])
        df["身份证-状态"] = results.apply(lambda x: x[3])
        
        valid_count = sum(1 for r in results if r[3] == "有效")
        print(f"   ✓ 完成，有效: {valid_count}，无效/格式错误: {len(results)-valid_count}")
    else:
        print("ℹ️ 未检测到身份证号列，跳过")
    
    print()
    
    # ============ IP地址处理 ============
    ip_col = None
    for col in ["IP", "IP地址", "ip", "ip_addr", "ip_address"]:
        if col in df.columns:
            ip_col = col
            break
    
    if ip_col:
        total = len(df)
        print(f"🔍 检测到IP列: 「{ip_col}」，开始查询（共{total}条，间隔{ip_delay}秒）...")
        
        ip_results = []
        for idx, ip in enumerate(df[ip_col]):
            ip_str = str(ip).strip()
            if ip_str and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
                location, provider = query_ip(ip_str, delay=ip_delay if idx > 0 else 0)
                ip_results.append((location, provider))
            else:
                ip_results.append(("格式错误", ""))
            
            if (idx + 1) % 50 == 0:
                print(f"   进度: {idx+1}/{total}")
        
        df["IP-归属地"] = [r[0] for r in ip_results]
        df["IP-运营商"] = [r[1] for r in ip_results]
        
        success = sum(1 for r in ip_results if "中国" in r[0] or " " in r[0] and r[0] != "格式错误" and r[0] != "连接错误")
        print(f"   ✓ 完成，成功: {success}，失败/格式错误: {total-success}")
    else:
        print("ℹ️ 未检测到IP列，跳过")
    
    print()
    
    # ============ 输出 ============
    out_path = fp.parent / f"{fp.stem}_结果.xlsx"
    df.to_excel(out_path, index=False, engine="openpyxl")
    print(f"✅ 已保存: {out_path}")
    print(f"   共 {len(df)} 行，{len(df.columns)} 列")
    
    return df


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 id_ip_query.py <文件路径> [--delay 秒数]")
        print("示例: python3 id_ip_query.py 名单.xlsx")
        print("      python3 id_ip_query.py 数据.csv --delay 0.5")
        sys.exit(1)
    
    filepath = sys.argv[1]
    delay = 1.0
    
    if "--delay" in sys.argv:
        idx = sys.argv.index("--delay")
        if idx + 1 < len(sys.argv):
            delay = float(sys.argv[idx + 1])
    
    process_file(filepath, ip_delay=delay)
