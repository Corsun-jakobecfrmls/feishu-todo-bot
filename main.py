"""
飞书 Todo 机器人 - 使用飞书多维表格作为数据库
"""

import os
import json
import time
import logging
import schedule
import threading
import requests
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_TARGET_OPEN_ID = os.environ.get("FEISHU_TARGET_OPEN_ID", "")
BITABLE_APP_TOKEN = os.environ.get("BITABLE_APP_TOKEN", "")
BITABLE_TABLE_ID = os.environ.get("BITABLE_TABLE_ID", "")

def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    return resp.json().get("tenant_access_token", "")

def bitable_headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

def get_todos():
    """获取所有未完成的待办"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{BITABLE_TABLE_ID}/records"
    params = {"filter": 'CurrentValue.[状态]!="完成"', "page_size": 100}
    resp = requests.get(url, headers=bitable_headers(), params=params, timeout=10)
    data = resp.json()
    records = data.get("data", {}).get("items", [])
    result = []
    for r in records:
        fields = r.get("fields", {})
        text = fields.get("任务内容", "")
        created = fields.get("创建时间", "")
        record_id = r.get("record_id", "")
        if text:
            result.append((record_id, text, created))
    return result

def add_todo(text):
    """添加待办"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{BITABLE_TABLE_ID}/records"
    payload = {"fields": {
        "任务内容": text,
        "状态": "待完成",
        "创建时间": datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    }}
    resp = requests.post(url, headers=bitable_headers(), json=payload, timeout=10)
    logger.info(f"添加待办: {resp.status_code}")

def mark_done(keyword):
    """模糊匹配关键词，标记完成"""
    todos = get_todos()
    matched = []
    for record_id, text, created in todos:
        if keyword in text:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{BITABLE_TABLE_ID}/records/{record_id}"
            requests.put(url, headers=bitable_headers(), json={"fields": {"状态": "完成"}}, timeout=10)
            matched.append(text)
    return matched

def send_text(open_id, text):
    token = get_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text})}
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    logger.info(f"发送消息: {resp.status_code}")

def send_todo_reminder(open_id):
    todos = get_todos()
    if not todos:
        send_text(open_id, "✅ 当前没有待办事项，继续保持！")
        return
    lines = ["📋 待办提醒\n"]
    for idx, (rid, text, created) in enumerate(todos, 1):
        lines.append(f"{idx}. 〇 {text}  ({created})")
    lines.append("\n💬 回复「完成 关键词」标记完成\n例如：完成 开会")
    send_text(open_id, "\n".join(lines))

def handle_message(open_id, text):
    text = text.strip()
    logger.info(f"收到消息 [{open_id}]: {text}")

    for prefix in ["完成", "做完", "done", "Done"]:
        if text.startswith(prefix):
            keyword = text[len(prefix):].strip()
            if keyword:
                matched = mark_done(keyword)
                if matched:
                    reply = "✅ 已完成：\n" + "\n".join(f"  · {t}" for t in matched)
                else:
                    reply = f"🔍 没找到包含「{keyword}」的待办事项"
                send_text(open_id, reply)
                return

    if text in ["查看", "列表", "list", "待办", "todo"]:
        send_todo_reminder(open_id)
        return

    if text in ["帮助", "help", "?"]:
        send_text(open_id, "📖 使用说明\n\n· 直接发任何内容 → 添加为待办\n· 「完成 关键词」→ 标记完成\n· 「查看」→ 查看所有待办\n\n每天 9:00 和 16:00 自动提醒 ⏰")
        return

    add_todo(text)
    count = len(get_todos())
    send_text(open_id, f"✨ 已添加待办：{text}\n\n当前共有 {count} 项待完成")

def scheduled_remind():
    if not FEISHU_TARGET_OPEN_ID:
        return
    logger.info("定时提醒触发")
    send_todo_reminder(FEISHU_TARGET_OPEN_ID)

def run_scheduler():
    schedule.every().day.at("01:00").do(scheduled_remind)  # 北京 09:00
    schedule.every().day.at("08:00").do(scheduled_remind)  # 北京 16:00
    while True:
        schedule.run_pending()
        time.sleep(30)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    header = data.get("header", {})
    event = data.get("event", {})

    if header.get("event_type") != "im.message.receive_v1":
        return jsonify({"code": 0})

    msg = event.get("message", {})
    sender = event.get("sender", {})
    open_id = sender.get("sender_id", {}).get("open_id", "")

    if msg.get("message_type") != "text":
        return jsonify({"code": 0})

    try:
        content = json.loads(msg.get("content", "{}"))
        text = content.get("text", "").strip()
        if text.startswith("@"):
            text = " ".join(text.split()[1:])
        if text and open_id:
            threading.Thread(target=handle_message, args=(open_id, text), daemon=True).start()
    except Exception as e:
        logger.error(f"消息处理出错: {e}")

    return jsonify({"code": 0})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/", methods=["GET"])
def index():
    return "飞书 Todo 机器人运行中 🤖"

threading.Thread(target=run_scheduler, daemon=True).start()
logger.info("飞书 Todo 机器人已启动 ✅")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
