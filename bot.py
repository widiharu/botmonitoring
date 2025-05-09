import os
import logging
import time
from datetime import datetime, timedelta
import requests
from telegram import Update, ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
# Load environment
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
CORTENSOR_API = os.getenv("CORTENSOR_API")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))

# In-memory storage. For production, switch to a persistent DB.
chats = {}  # chat_id: {"nodes": [{"address":..., "label":...}], "delay": 60}

# Setup logging
tlogging = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Helper: fetch node transactions
def fetch_transactions(address, limit=25):
    url = f"https://api.arbiscan.io/api"
    params = {
        'module': 'account',
        'action': 'txlist',
        'address': address,
        'startblock': 0,
        'endblock': 99999999,
        'page': 1,
        'offset': limit,
        'sort': 'desc',
        'apikey': API_KEY
    }
    resp = requests.get(url, params=params).json()
    return resp.get('result', [])

# Helper: compute status
METHODS = {
    '0xf21a494b': 'Commit',
    '0x65c815a5': 'Precommit',
    '0xca6726d9': 'Prepare',
    '0x198e2b8a': 'Create',
    '0x5c36b186': 'PING'
}

def analyze_node(node):
    addr = node['address']
    txs = fetch_transactions(addr)
    balance = requests.get(f"{CORTENSOR_API}/balance/{addr}").json().get('balance')

    # Status
    now = datetime.utcnow()
    last_tx_time = datetime.utcfromtimestamp(int(txs[0]['timeStamp'])) if txs else None
    status = '🟢 Online' if last_tx_time and (now - last_tx_time) < timedelta(minutes=15) else '🔴 Offline'

    # Health indicators
    health = []
    successes = 0
    for tx in txs:
        method = METHODS.get(tx['input'][:10], 'Other')
        ok = tx.get('isError') == '0'
        if ok: successes += 1
        health.append('🟩' if ok else '🟥')
    health = health[:5]

    # Stall detection
    if all(tx['input'][:10] == '0x5c36b186' for tx in txs):
        # find most recent non-PING success
        for tx in txs:
            if tx['input'][:10] != '0x5c36b186' and tx.get('isError') == '0':
                last_ok = datetime.utcfromtimestamp(int(tx['timeStamp']))
                delta = now - last_ok
                tx_note = f"Last successful {METHODS.get(tx['input'][:10], 'Tx')} was {int(delta.total_seconds()//60)} mins ago"
                break
    else:
        tx_note = f"Last successful {METHODS.get(txs[0]['input'][:10], 'Tx')} was {(now-last_tx_time).seconds//60} mins ago"

    return {
        'address': addr,
        'label': node.get('label', ''),
        'balance': f"{float(balance):.4f} ETH",
        'status': status,
        'last_activity': f"{int((now-last_tx_time).seconds//60)} mins ago" if last_tx_time else 'N/A',
        'health': ''.join(health),
        'stall': '⚠️ Stall' if '🟥' in health else '✅ Normal',
        'tx_note': tx_note
    }

async def send_status(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    cfg = chats.get(chat_id)
    if not cfg: return
    text_parts = []
    for node in cfg['nodes']:
        st = analyze_node(node)
        text = (
            f"🔑 {st['address']} ({node.get('label','')})\n"
            f"💰 Balance: {st['balance']} | Status: {st['status']}\n"
            f"⏱️ Last Activity: {st['last_activity']}\n"
            f"🩺 Health: {st['health']}\n"
            f"⚠️ Stall: {st['stall']}\n"
            f"Transaction: {st['tx_note']}\n"
            f"🔗 <a href='https://arbiscan.io/address/{st['address']}'>Arbiscan</a> | 📈 <a href='{CORTENSOR_API}/dashboard/{st['address']}'>Dashboard</a>"
        )
        text_parts.append(text)
    await context.bot.send_message(chat_id=chat_id, text='\n\n'.join(text_parts), parse_mode=ParseMode.HTML)

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats.setdefault(update.effective_chat.id, {'nodes': [], 'delay': 60})
    await update.message.reply_text("Welcome! Use /addaddress to track a node.")

async def addaddress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(context.args)
    try:
        address,label = text.split(',')
    except ValueError:
        await update.message.reply_text("Format: /addaddress <address>,<label>")
        return
    cfg = chats.setdefault(update.effective_chat.id, {'nodes': [], 'delay': 60})
    cfg['nodes'].append({'address': address.strip(), 'label': label.strip()})
    await update.message.reply_text(f"Added {address} as {label}.")

async def setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(context.args[0])
        if delay < 60: raise ValueError
    except Exception:
        await update.message.reply_text("Usage: /setdelay <seconds> (>=60)")
        return
    cfg = chats.setdefault(update.effective_chat.id, {'nodes': [], 'delay': 60})
    cfg['delay'] = delay
    # reschedule job
    job = scheduler.get_job(str(update.effective_chat.id))
    if job: job.reschedule(trigger=IntervalTrigger(seconds=delay))
    await update.message.reply_text(f"Update interval set to {delay} seconds.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_status(context)

# Admin broadcast
async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    text = ' '.join(context.args)
    for chat_id in chats:
        await context.bot.send_message(chat_id=chat_id, text=f"📢 {text}")
    await update.message.reply_text("Announcement sent.")

# Setup bot
app = ApplicationBuilder().token(TOKEN).build()
scheduler = AsyncIOScheduler()
scheduler.start()

# Schedule jobs for existing chats
def schedule_for_chat(chat_id, delay):
    scheduler.add_job(send_status, IntervalTrigger(seconds=delay), args=[app.bot], id=str(chat_id), replace_existing=True)

# Register handlers
app.add_handler(CommandHandler('start', start))
app.add_handler(CommandHandler('addaddress', addaddress))
app.add_handler(CommandHandler('setdelay', setdelay))
app.add_handler(CommandHandler('status', status))
app.add_handler(CommandHandler('announce', announce))

# On startup schedule existing
for cid, cfg in chats.items():
    schedule_for_chat(cid, cfg['delay'])

if __name__ == '__main__':
    app.run_polling()