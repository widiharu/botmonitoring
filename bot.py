import os
import logging
import time
from datetime import datetime, timedelta
import requests
from telegram import ParseMode
from telegram.ext import (Updater, CommandHandler, CallbackContext)

# Load environment variables
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
CORTENSOR_API = os.getenv("CORTENSOR_API")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))

# In-memory storage. For production, switch to a persistent DB.
chats = {}  # chat_id: {"nodes": [{"address":..., "label":...}], "delay": 60}

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Helper: fetch node transactions
def fetch_transactions(address, limit=25):
    url = "https://api.arbiscan.io/api"
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

# Mapping method IDs
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
    balance_resp = requests.get(f"{CORTENSOR_API}/balance/{addr}").json()
    balance = balance_resp.get('balance', 0)

    now = datetime.utcnow()
    last_tx_time = datetime.utcfromtimestamp(int(txs[0]['timeStamp'])) if txs else None
    status = '🟢 Online' if last_tx_time and (now - last_tx_time) < timedelta(minutes=15) else '🔴 Offline'

    # Health indicators (first 5 transactions)
    health_icons = []
    for tx in txs[:5]:
        ok = tx.get('isError') == '0'
        health_icons.append('🟩' if ok else '🟥')

    # Stall detection over last 25
    stall_flag = False
    if txs and all(tx['input'][:10] == '0x5c36b186' for tx in txs):
        stall_flag = True
        # find recent non-PING success
        note = 'No recent non-PING tx found'
        for tx in txs:
            if tx['input'][:10] != '0x5c36b186' and tx.get('isError') == '0':
                t = datetime.utcfromtimestamp(int(tx['timeStamp']))
                delta = now - t
                mins = int(delta.total_seconds() // 60)
                note = f"Last successful {METHODS.get(tx['input'][:10], 'Tx')} was {mins} mins ago"
                break
    else:
        note = 'N/A'

    last_activity = f"{int((now - last_tx_time).seconds // 60)} mins ago" if last_tx_time else 'N/A'
    return {
        'address': addr,
        'label': node.get('label', ''),
        'balance': f"{float(balance):.4f} ETH",
        'status': status,
        'last_activity': last_activity,
        'health': ''.join(health_icons) or 'N/A',
        'stall': '⚠️ Stall' if stall_flag else '✅ Normal',
        'tx_note': note
    }

# Periodic status sender
def send_status(context: CallbackContext):
    job = context.job
    chat_id = job.context['chat_id']
    cfg = chats.get(chat_id)
    if not cfg or not cfg['nodes']:
        return

    messages = []
    for node in cfg['nodes']:
        st = analyze_node(node)
        msg = (
            f"🔑 {st['address']} ({st['label']})\n"
            f"💰 Balance: {st['balance']} | Status: {st['status']}\n"
            f"⏱️ Last Activity: {st['last_activity']}\n"
            f"🩺 Health: {st['health']}\n"
            f"⚠️ Stall: {st['stall']}\n"
            f"Transaction: {st['tx_note']}\n"
            f"🔗 <a href='https://arbiscan.io/address/{st['address']}'>Arbiscan</a> | 📈 <a href='{CORTENSOR_API}/dashboard/{st['address']}'>Dashboard</a>"
        )
        messages.append(msg)

    context.bot.send_message(chat_id=chat_id, text='\n\n'.join(messages), parse_mode=ParseMode.HTML)

# Command handlers
def start(update, context):
    chat_id = update.effective_chat.id
    chats.setdefault(chat_id, {'nodes': [], 'delay': 60})
    update.message.reply_text("Welcome! Use /addaddress <address>,<label> to track nodes.")

def addaddress(update, context):
    chat_id = update.effective_chat.id
    text = ' '.join(context.args)
    try:
        address, label = map(str.strip, text.split(','))
    except ValueError:
        update.message.reply_text("Format: /addaddress <address>,<label>")
        return
    cfg = chats.setdefault(chat_id, {'nodes': [], 'delay': 60})
    if len(cfg['nodes']) >= 25:
        update.message.reply_text("Max 25 nodes per chat reached.")
        return
    cfg['nodes'].append({'address': address, 'label': label})
    update.message.reply_text(f"Added node {label} ({address}).")

def setdelay(update, context):
    chat_id = update.effective_chat.id
    try:
        delay = int(context.args[0])
        if delay < 60:
            raise ValueError
    except:
        update.message.reply_text("Usage: /setdelay <seconds> (minimum 60)")
        return

    cfg = chats.setdefault(chat_id, {'nodes': [], 'delay': 60})
    cfg['delay'] = delay
    # reschedule job
    job_name = str(chat_id)
    context.job_queue.get_jobs_by_name(job_name)[0].schedule_removal()
    context.job_queue.run_repeating(send_status, interval=delay, first=0,
                                    context={'chat_id': chat_id}, name=job_name)
    update.message.reply_text(f"Update interval set to {delay} seconds.")

def status(update, context):
    # Trigger immediate status
    context.job_queue.run_once(send_status, when=0, context={'chat_id': update.effective_chat.id})

def announce(update, context):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        update.message.reply_text("Unauthorized.")
        return
    msg = ' '.join(context.args)
    for cid in chats.keys():
        context.bot.send_message(cid, f"📢 {msg}")
    update.message.reply_text("Announcement sent.")

if __name__ == '__main__':
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Register handlers
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('addaddress', addaddress))
    dp.add_handler(CommandHandler('setdelay', setdelay))
    dp.add_handler(CommandHandler('status', status))
    dp.add_handler(CommandHandler('announce', announce))

    # Schedule jobs for existing chats
    for chat_id, cfg in chats.items():
        updater.job_queue.run_repeating(send_status, interval=cfg['delay'], first=0,
                                        context={'chat_id': chat_id}, name=str(chat_id))

    updater.start_polling()
    updater.idle()
