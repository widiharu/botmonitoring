#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Inline Keyboard Version
Commands via inline buttons or slash.
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    ParseMode,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    ConversationHandler,
)

# Load config
load_dotenv()
TOKEN         = os.getenv("TOKEN")
API_KEY       = os.getenv("API_KEY")
CORTENSOR_API = os.getenv(
    "CORTENSOR_API",
    "https://dashboard-devnet3.cortensor.network"
)
ADMIN_IDS     = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
MAX_NODES     = int(os.getenv("MAX_ADDRESS_PER_CHAT", 5))
DATA_FILE     = "data.json"
DEFAULT_INT   = 300
MIN_INT       = 60

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# Persistence
def load_data():
    if os.path.exists(DATA_FILE):
        return json.load(open(DATA_FILE))
    return {}

def save_data(d):
    json.dump(d, open(DATA_FILE, "w"), indent=2)

def get_chat(cid):
    d = load_data()
    return d.setdefault(str(cid), {"nodes": [], "interval": DEFAULT_INT})

def update_chat(cid, data):
    d = load_data()
    d[str(cid)] = data
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

# Etherscan
def fetch_txs(addr):
    try:
        r = requests.get(
            "https://api-sepolia.arbiscan.io/api",
            params={
                "module": "account", "action": "txlist",
                "address": addr, "sort": "desc",
                "page": 1, "offset": 100, "apikey": API_KEY
            },
            timeout=10
        ).json().get("result", [])
        return r if isinstance(r, list) else []
    except:
        return []

def fetch_balance(addr):
    try:
        r = requests.get(
            "https://api-sepolia.arbiscan.io/api",
            params={
                "module": "account", "action": "balance",
                "address": addr, "tag": "latest", "apikey": API_KEY
            },
            timeout=10
        ).json().get("result", "0")
        return int(r) / 1e18
    except:
        return 0.0

# Method mapping
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

def build_report(n, i):
    addr = n["wallet"]
    label = n.get("label", f"Node {i}")
    txs = fetch_txs(addr)
    bal = fetch_balance(addr)
    last_tx = int(txs[0]["timeStamp"]) if txs else 0
    status = "🟢 Online" if (datetime.now() - datetime.fromtimestamp(last_tx)) < timedelta(minutes=5) else "🔴 Offline"
    last_act = age(last_tx) if txs else "N/A"

    # Health
    groups = [txs[j * 5:(j + 1) * 5] for j in range(5)]
    health = " ".join(
        "🟩" if grp and all(t.get("isError") == "0" for t in grp)
        else "⬜" if not grp
        else "🟥"
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

# Conversation states
ADD, REM, DELAY = range(3)

# Inline menu
MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ Add", callback_data="add")],
    [InlineKeyboardButton("➖ Remove", callback_data="remove")],
    [InlineKeyboardButton("📋 List", callback_data="list")],
    [InlineKeyboardButton("📊 Status", callback_data="status")],
    [InlineKeyboardButton("🔄 Auto", callback_data="auto")],
    [InlineKeyboardButton("🔔 Alerts", callback_data="alerts")],
    [InlineKeyboardButton("⏱️ Delay", callback_data="delay")],
    [InlineKeyboardButton("⏹ Stop", callback_data="stop")],
    [InlineKeyboardButton("📣 Announce", callback_data="announce")],
])

# Handlers
def start(update: Update, ctx: CallbackContext):
    update.message.reply_text("Choose an option:", reply_markup=MENU)

def button(update: Update, ctx: CallbackContext):
    q = update.callback_query
    q.answer()
    data = q.data

    if data == "add":
        q.message.reply_text("Send address[,label]:", reply_markup=ForceReply())
        return ADD
    if data == "remove":
        q.message.reply_text("Send address to remove:", reply_markup=ForceReply())
        return REM
    if data == "delay":
        q.message.reply_text("Send interval in seconds:", reply_markup=ForceReply())
        return DELAY
    if data == "list":
        nodes = get_chat(q.message.chat_id)["nodes"]
        text = "\n".join(
            f"- {n.get('label') or shorten(n['wallet'])}: {n['wallet']}"
            for n in nodes
        ) or "No nodes."
        q.message.reply_text(text, reply_markup=MENU)
        return ConversationHandler.END
    if data == "status":
        nodes = get_chat(q.message.chat_id)["nodes"]
        msg = "Auto Update\n\n"
        for i, n in enumerate(nodes, 1):
            msg += build_report(n, i) + "\n"
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            q.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=MENU)
        return ConversationHandler.END
    # auto/alerts/stop/announce can simply call status or broadcast
    q.message.reply_text("Use /status or /announce <msg> for those features.", reply_markup=MENU)
    return ConversationHandler.END

def handle_add(update: Update, ctx: CallbackContext):
    cid = update.effective_chat.id
    text = update.message.text.strip()
    wallet, *label = text.split(",", 1)
    label = label[0].strip() if label else None
    chat = get_chat(cid)
    if len(chat["nodes"]) < MAX_NODES:
        chat["nodes"].append({"wallet": wallet, "label": label})
        update_chat(cid, chat)
        update.message.reply_text(f"Added {label or shorten(wallet)}", reply_markup=MENU)
    else:
        update.message.reply_text(f"Max {MAX_NODES} nodes reached.", reply_markup=MENU)
    return ConversationHandler.END

def handle_remove(update: Update, ctx: CallbackContext):
    cid = update.effective_chat.id
    wallet = update.message.text.strip()
    chat = get_chat(cid)
    chat["nodes"] = [n for n in chat["nodes"] if n["wallet"] != wallet]
    update_chat(cid, chat)
    update.message.reply_text(f"Removed {wallet}", reply_markup=MENU)
    return ConversationHandler.END

def handle_delay(update: Update, ctx: CallbackContext):
    cid = update.effective_chat.id
    try:
        sec = int(update.message.text.strip())
        if sec < MIN_INT:
            raise ValueError()
        chat = get_chat(cid)
        chat["interval"] = sec
        update_chat(cid, chat)
        update.message.reply_text(f"Interval set to {sec}s", reply_markup=MENU)
    except:
        update.message.reply_text(f"Enter a number ≥ {MIN_INT}", reply_markup=MENU)
    return ConversationHandler.END

def cancel(update: Update, ctx: CallbackContext):
    update.message.reply_text("Cancelled", reply_markup=MENU)
    return ConversationHandler.END

# Main
def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button)],
        states={
            ADD: [MessageHandler(Filters.text & ~Filters.command, handle_add)],
            REM: [MessageHandler(Filters.text & ~Filters.command, handle_remove)],
            DELAY: [MessageHandler(Filters.text & ~Filters.command, handle_delay)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(conv)

    updater.start_polling()
    logger.info("Bot running…")
    updater.idle()

if __name__ == "__main__":
    main()