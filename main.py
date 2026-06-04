"""
飞书 Todo 机器人后端
功能：接收消息 → 智能解析为待办 → 每天9点/21点推送提醒 → 支持完成标记
"""

import os
import json
import time
import hmac
import hashlib
import base64
import sqlite3
import logging
import schedule
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 配置（从环境变量读取，部署时填入）
# ─────────────────────────────────────────────
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
# 推送目标：填你的飞书 open_id 或 chat_id
FEISHU_TARGET_OPEN_ID = os.environ.get("FEISHU_TARGET_OPEN_ID", "")
DB_PATH = os.environ.get("DB_PATH", "todos.db")

# ─────────────────────────────────────────────
# 数据库初始化
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            text      TEXT    NOT NULL,
            done      INTEGER NOT NULL DEFAULT 0,
            created   TEXT    NOT NULL,
            done_time TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_todos(done=0):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text, created FROM todos WHERE done=? ORDER BY id DESC",
        (done,)
    ).fetchall()
    conn.close()
    return rows

def add_todo(text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO todos (text, done, created) VALUES (?, 0, ?)",
        (text, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()

def mark_done_by_keyword(keyword):
    """模糊匹配关键词，标记对应任务为完成"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, text FROM todos WHERE done=0 AND text LIKE ?",
        (f"%{keyword}%",)
    ).fetchall()
    matched = []
    for row in rows:
        conn.execute(
            "UPDATE todos SET done=1, done_time=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), row[0])
        )
        matched.append(row[1])
    conn.commit()
    conn.close()
    return matched

# ─────────────────────────────────────────────
# 飞书 API：获取 tenant_access_token
# ─────────────────────────────────────────────
def get_tenant_access_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET
    }, timeout=10)
    return resp.json().get("tenant_access_token", "")

# ─────────────────────────────────────────────
# 飞书 API：发送消息
# ─────────────────────────────────────────────
def send_feishu_message(open_id, content, msg_type="text"):
    token = get_tenant_access_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "receive_id": open_id,
        "msg_type": msg_type,
        "content": json.dumps(content)
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    logger.info(f"发送消息结果: {resp.status_code} {resp.text[:200]}")
    return resp.json()

def send_text(open_id, text):
    send_feishu_message(open_id, {"text": text})

def send_todo_card(open_id, todos):
    """发送富文本卡片，展示待办列表"""
    if not todos:
        send_text(open_id, "✅ 当前没有待办事项，继续保持！")
        return

    lines = ["📋 **待办提醒**\n"]
    for idx, (tid, text, created) in enumerate(todos, 1):
        lines.append(f"{idx}. 〇 {text}  _(添加于 {created})_")
    lines.append("\n💬 回复「完成 关键词」来标记任务完成\n例如：完成 开会")

    send_text(open_id, "\n".join(lines))

# ─────────────────────────────────────────────
# 解析飞书消息 & 处理逻辑
# ─────────────────────────────────────────────
def handle_message(open_id, text):
    text = text.strip()
    logger.info(f"收到消息 [{open_id}]: {text}")

    # 完成指令：完成 xxx / 做完了 xxx / done xxx
    finish_prefixes = ["完成", "做完", "done", "Done", "✅"]
    for prefix in finish_prefixes:
        if text.startswith(prefix):
            keyword = text[len(prefix):].strip()
            if keyword:
                matched = mark_done_by_keyword(keyword)
                if matched:
                    reply = f"✅ 已完成：\n" + "\n".join(f"  · {t}" for t in matched)
                else:
                    reply = f"🔍 没找到包含「{keyword}」的待办事项"
                send_text(open_id, reply)
                return

    # 查看指令
    if text in ["查看", "列表", "list", "待办", "todo"]:
        todos = get_todos(done=0)
        send_todo_card(open_id, todos)
        return

    # 帮助指令
    if text in ["帮助", "help", "?"]:
        help_text = (
            "📖 使用说明\n\n"
            "· 直接发任何内容 → 添加为待办\n"
            "· 「完成 关键词」→ 标记任务完成\n"
            "· 「查看」→ 查看所有待办\n\n"
            "我会每天 9:00 和 16:00 提醒你未完成的事项 ⏰"
        )
        send_text(open_id, help_text)
        return

    # 默认：添加为待办
    add_todo(text)
    todos_count = len(get_todos(done=0))
    send_text(open_id, f"✨ 已添加待办：{text}\n\n当前共有 {todos_count} 项待完成")

# ─────────────────────────────────────────────
# 定时任务：每天9点、21点推送
# ─────────────────────────────────────────────
def scheduled_remind():
    if not FEISHU_TARGET_OPEN_ID:
        logger.warning("未设置 FEISHU_TARGET_OPEN_ID，跳过定时提醒")
        return
    todos = get_todos(done=0)
    logger.info(f"定时提醒触发，共 {len(todos)} 项待办")
    send_todo_card(FEISHU_TARGET_OPEN_ID, todos)

def run_scheduler():
    schedule.every().day.at("01:00").do(scheduled_remind)  # UTC 01:00 = 北京 09:00
    schedule.every().day.at("08:00").do(scheduled_remind)  # UTC 08:00 = 北京 16:00
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────
# 飞书 Webhook 接收
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    # 1. URL 验证（飞书首次配置时发送）
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    # 2. 处理事件
    header = data.get("header", {})
    event = data.get("event", {})

    # 只处理接收消息事件
    if header.get("event_type") != "im.message.receive_v1":
        return jsonify({"code": 0})

    msg = event.get("message", {})
    sender = event.get("sender", {})
    open_id = sender.get("sender_id", {}).get("open_id", "")

    # 只处理文本消息
    if msg.get("message_type") != "text":
        return jsonify({"code": 0})

    try:
        content = json.loads(msg.get("content", "{}"))
        text = content.get("text", "").strip()
        # 去掉 @机器人 的部分
        if text.startswith("@"):
            text = " ".join(text.split()[1:])
        if text and open_id:
            threading.Thread(
                target=handle_message, args=(open_id, text), daemon=True
            ).start()
    except Exception as e:
        logger.error(f"消息处理出错: {e}")

    return jsonify({"code": 0})

@app.route("/health", methods=["GET"])
def health():
    todos = get_todos(done=0)
    return jsonify({"status": "ok", "pending_todos": len(todos)})

@app.route("/", methods=["GET"])
def index():
    return "飞书 Todo 机器人运行中 🤖"

# ─────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    # 启动定时任务线程
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("飞书 Todo 机器人已启动 ✅")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
