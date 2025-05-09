#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Reply Keyboard Version

Commands:
• /add <wallet_address>[,label]  – Add node with optional label
• /remove <wallet_address>       – Remove node
• /list                          – List monitored nodes
• /status                        – Show combined status now
• /auto                          – Start auto-update
• /alerts                        – Enable alerts
• /delay <seconds>               – Set auto-update interval
• /stop                          – Stop auto-updates & alerts
• /announce <message>            – (Admin only) Broadcast announcement

Max nodes per chat: 5
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext

# Load config
load_dotenv()
TOKEN            = os.getenv("TOKEN")
API_KEY          = os.getenv("API_KEY")
CORTENSOR_API    = os.getenv("CORTENSOR_API", "https://dashboard-devnet3.cortensor.network")
ADMIN_IDS        = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x]
MAX_NODES        = int(os.getenv("MAX_ADDRESS_PER_CHAT", 5))
DATA_FILE        = "data.json"
DEFAULT_INTERVAL = 300  # seconds
MIN_INTERVAL     = 60   # seconds

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Data persistence

def load_data():
    if os.path.exists(DATA_FILE):
        return json.load(open(DATA_FILE))
    return {}


def save_data(d):
    json.dump(d, open(DATA_FILE, "w"), indent=2)


def get_chat(chat_id):
    d = load_data()
    return d.setdefault(str(chat_id), {"nodes": [], "interval": DEFAULT_INTERVAL})


def update_chat(chat_id, data):
    d = load_data()
    d[str(chat_id)] = data
    save_data(d)

# Helpers

def shorten(addr):
    return addr[:6] + "..." + addr[-4:]


def age(ts):
    delta = datetime.now() - datetime.fromtimestamp(ts)
    if delta.days > 0:
        return f"{delta.days}d {delta.seconds//3600}h ago"
    h = delta.seconds // 3600
    m = (delta.seconds % 3600) // 60
    return f"{h}h {m}m ago" if h else f"{m}m ago"


def fetch_txs(addr):
    url = "https://api-sepolia.arbiscan.io/api"
    params = {
        "module": "account", "action": "txlist",
        "address": addr, "sort": "desc",
        "page": 1, "offset": 100, "apikey": API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10).json().get("result", [])
        return r if isinstance(r, list) else []
    except:
        return []


def fetch_balance(addr):
    url = "https://api-sepolia.arbiscan.io/api"
    params = {
        "module": "account", "action": "balance",
        "address": addr, "tag": "latest", "apikey": API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10).json().get("result", "0")
        return int(r)/1e18
    except:
        return 0.0

# Method definitions
METHODS = {
    "0xf21a494b": "Commit",
    "0x65c815a5": "Precommit",
    "0xca6726d9": "Prepare",
    "0x198e2b8a": "Create"
}
PING = "0x5c36b186"


def last_successful(txs):
    for tx in txs:
        inp = tx.get("input", "")
        if inp.startswith(PING) or tx.get("isError") != "0":
            continue
        m = inp[:10]
        if m in METHODS:
            return METHODS[m], int(tx["timeStamp"])
    return None, None


def build_report(node, idx):
    addr = node["wallet"]
    label = node.get("label", f"Node {idx}")
    txs = fetch_txs(addr)
    bal = fetch_balance(addr)
    last_tx = int(txs[0]["timeStamp"]) if txs else 0
    status = "🟢 Online" if (datetime.now() - datetime.fromtimestamp(last_tx)) < timedelta(minutes=5) else "🔴 Offline"
    last_act = age(last_tx) if txs else "N/A"

    # Health
    groups = [txs[i*5:(i+1)*5] for i in range(5)]
    health = " ".join(
        "🟩" if grp and all(t.get("isError") == "0" for t in grp) else
        "⬜" if not grp else "🟥"
        for grp in groups
    )

    # Stall
    last25 = txs[:25]
    stalled = bool(last25) and all(t.get("input", "").startswith(PING) for t in last25)
    name, ts = last_successful(txs)
    note = f"(last successful {name} {age(ts)})" if name else ""
    stall_txt = f"🚨 Stalled {note}" if stalled else "✅ Normal"

    return (
        f"🔑 {shorten(addr)} ({label})\n"
        f"💰 Balance: {bal:.4f} ETH | {status}\n"
        f"⏱️ Last Activity: {last_act}\n"
        f"🩺 Health: {health}\n"
        f"⚠️ Stall: {stall_txt}\n"
        f"🔗 https://sepolia.arbiscan.io/address/{addr}\n"
        f"📈 {CORTENSOR_API}/stats/node/{addr}\n"
    )

# Command handlers

def start(update: Update, ctx: CallbackContext):
    update.message.reply_text("👋 Welcome to Cortensor Node Monitor! Use /help to see commands.")


def help_cmd(update: Update, ctx: CallbackContext):
    update.message.reply_text(
        "/add <wallet>[,label]\n"
        "/remove <wallet>\n"
        "/list\n"
        "/status\n"
        "/auto\n"
        "/alerts\n"
        "/delay <sec>\n"
        "/stop\n"
        "/announce <msg>\n"
    )


def add(update: Update, ctx: CallbackContext):
    chat_id = update.effective_chat.id
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        return update.message.reply_text("Usage: /add <wallet>[,label]")
    wallet, label = (parts[1].split(",",1) + [None])[:2]
    chat = get_chat(chat_id)
    if len(chat["nodes"]) >= MAX_NODES:
        return update.message.reply_text(f"Max {MAX_NODES} reached.")
    chat["nodes"].append({"wallet": wallet.strip(), "label": label.strip() if label else None})
    update_chat(chat_id, chat)
    update.message.reply_text(f"✅ Added: {label or shorten(wallet)}")


def remove(update: Update, ctx: CallbackContext):
    chat_id = update.effective_chat.id
    if not ctx.args:
        return update.message.reply_text("Usage: /remove <wallet>")
    wallet = ctx.args[0]
    chat = get_chat(chat_id)
    chat["nodes"] = [n for n in chat["nodes"] if n["wallet"] != wallet]
    update_chat(chat_id, chat)
    update.message.reply_text(f"✅ Removed: {wallet}")


def list_cmd(update: Update, ctx: CallbackContext):
    nodes = get_chat(update.effective_chat.id)["nodes"]
    if not nodes:
        return update.message.reply_text("No nodes registered.")
    text = "\n".join(f"- {n.get('label') or shorten(n['wallet'])}: {n['wallet']}" for n in nodes)
    update.message.reply_text(text)


def status(update: Update, ctx: CallbackContext):
    nodes = get_chat(update.effective_chat.id)["nodes"]
    if not nodes:
        return update.message.reply_text("No nodes registered.")
    msg = "Auto Update\n\n"
    for i,n in enumerate(nodes, 1):
        msg += build_report(n, i) + "\n"
    for chunk in [msg[i:i+4000] for i in range(0,len(msg),4000)]:
        update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)


def auto(update: Update, ctx: CallbackContext):
    chat = get_chat(update.effective_chat.id)
    if 'job_auto' in ctx.chat_data:
        return update.message.reply_text("⚠️ Auto-updates already active.")
    job = ctx.job_queue.run_repeating(
        lambda c: status(update, ctx),
        interval=chat['interval'], first=0)
    ctx.chat_data['job_auto'] = job
    update.message.reply_text("✅ Auto-update started.")


def alerts(update: Update, ctx: CallbackContext):
    if 'job_alert' in ctx.chat_data:
        return update.message.reply_text("⚠️ Alerts already active.")
    job = ctx.job_queue.run_repeating(
        lambda c: status(update, ctx),
        interval=900, first=0)
    ctx.chat_data['job_alert'] = job
    update.message.reply_text("✅ Alerts enabled.")


def delay(update: Update, ctx: CallbackContext):
    if not ctx.args or not ctx.args[0].isdigit():
        return update.message.reply_text("Usage: /delay <seconds>")
    sec = int(ctx.args[0])
    if sec < MIN_INTERVAL:
        return update.message.reply_text(f"Minimum interval is {MIN_INTERVAL}s.")
    chat = get_chat(update.effective_chat.id)
    chat['interval'] = sec
    update_chat(update.effective_chat.id, chat)
    update.message.reply_text(f"✅ Interval set to {sec}s.")


def stop(update: Update, ctx: CallbackContext):
    for k in ('job_auto','job_alert'):
        job = ctx.chat_data.pop(k, None)
        if job:
            job.schedule_removal()
    update.message.reply_text("✅ Stopped all jobs.")


def announce(update: Update, ctx: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        return update.message.reply_text("❌ Unauthorized.")
    msg = update.message.text.split(' ',1)[1] if ' ' in update.message.text else ''
    d = load_data()
    for cid in d:
        ctx.bot.send_message(int(cid), msg)
    update.message.reply_text("📣 Announcement sent.")

# Main
def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("remove", remove))
    dp.add_handler(CommandHandler("list", list_cmd))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("auto", auto))
    dp.add_handler(CommandHandler("alerts", alerts))
    dp.add_handler(CommandHandler("delay", delay))
    dp.add_handler(CommandHandler("stop", stop))
    dp.add_handler(CommandHandler("announce", announce))

    updater.start_polling()
    logger.info("Bot is running...")
    updater.idle()

if __name__ == "__main__":
    main()