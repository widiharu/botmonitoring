#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Reply Keyboard Version

Commands:
• /add <wallet_address>[,label]  – Add node with optional label
• /remove <wallet_address>       – Remove node
• /list                          – List monitored nodes
• /check                         – Check status now
• /auto                          – Start auto-update
• /alerts                        – Enable alerts
• /delay <seconds>               – Set auto-update interval
• /stop                          – Stop auto-updates & alerts
• /announce <message>            – (Admin) Broadcast announcement
Max nodes per chat: 5
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ParseMode, ReplyKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# Load .env
load_dotenv()
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
CORTENSOR_API = os.getenv("CORTENSOR_API")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS","" ).split(",") if x]
MAX_NODES = int(os.getenv("MAX_ADDRESS_PER_CHAT", 5))
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 60
DATA_FILE = "data.json"

# States for ConversationHandlers
ADD, = range(1)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Utilities

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

# Fetch helpers

def fetch_txs(addr):
    url = "https://api-sepolia.arbiscan.io/api"
    params = {"module":"account","action":"txlist","address":addr,
              "sort":"desc","page":1,"offset":100,"apikey":API_KEY}
    try:
        res = requests.get(url, params=params, timeout=10).json().get("result", [])
        return res if isinstance(res,list) else []
    except:
        return []

# Command handlers

def start(update: Update, ctx: CallbackContext):
    update.message.reply_text("Welcome! Use /help to see commands.")


def help_cmd(update: Update, ctx: CallbackContext):
    text = (
        "/add <wallet>[,label] - Add node\n"
        "/remove <wallet>     - Remove node\n"
        "/list                - List nodes\n"
        "/check               - Check now\n"
        "/auto                - Start auto updates\n"
        "/alerts              - Enable alerts\n"
        "/delay <seconds>     - Set interval\n"
        "/stop                - Stop jobs\n"
        "/announce <msg>      - Admin broadcast\n"
    )
    update.message.reply_text(text)


def add_cmd(update: Update, ctx: CallbackContext):
    text = update.message.text.split(maxsplit=1)
    if len(text)<2:
        return update.message.reply_text("Usage: /add <wallet>[,label]")
    arg = text[1].split(',',1)
    wallet = arg[0]
    label = arg[1] if len(arg)>1 else wallet[:6]
    chat = get_chat(update.effective_chat.id)
    if len(chat['nodes'])>=MAX_NODES:
        return update.message.reply_text(f"Max {MAX_NODES} nodes reached.")
    chat['nodes'].append({'wallet':wallet,'label':label})
    update_chat(update.effective_chat.id, chat)
    update.message.reply_text(f"Added {label}: {wallet}")


def remove_cmd(update: Update, ctx: CallbackContext):
    if len(ctx.args)!=1:
        return update.message.reply_text("Usage: /remove <wallet>")
    wallet = ctx.args[0]
    chat = get_chat(update.effective_chat.id)
    chat['nodes'] = [n for n in chat['nodes'] if n['wallet']!=wallet]
    update_chat(update.effective_chat.id, chat)
    update.message.reply_text(f"Removed {wallet}")


def list_cmd(update: Update, ctx: CallbackContext):
    nodes = get_chat(update.effective_chat.id)['nodes']
    if not nodes:
        return update.message.reply_text("No nodes.")
    text = "".join(f"- {n['label']}: {n['wallet']}\n" for n in nodes)
    update.message.reply_text(text)


def check_cmd(update: Update, ctx: CallbackContext):
    chat = get_chat(update.effective_chat.id)
    for n in chat['nodes']:
        txs = fetch_txs(n['wallet'])[:25]
        stall = all(tx['input'].startswith('0x5c36b186') for tx in txs)
        status = 'Stalled' if stall else 'Active'
        update.message.reply_text(f"{n['label']} - {status}")


def auto_cmd(update: Update, ctx: CallbackContext):
    chat_id = update.effective_chat.id
    chat = get_chat(chat_id)
    if 'job_auto' in ctx.chat_data:
        return update.message.reply_text("Already running")
    job = ctx.job_queue.run_repeating(lambda c: check_cmd(update,ctx), interval=chat['interval'], first=0)
    ctx.chat_data['job_auto'] = job
    update.message.reply_text("Auto update started.")


def alerts_cmd(update: Update, ctx: CallbackContext):
    chat_id = update.effective_chat.id
    if 'job_alerts' in ctx.chat_data:
        return update.message.reply_text("Alerts already on")
    job = ctx.job_queue.run_repeating(lambda c: check_cmd(update,ctx), interval=900, first=0)
    ctx.chat_data['job_alerts'] = job
    update.message.reply_text("Alerts enabled.")


def delay_cmd(update: Update, ctx: CallbackContext):
    if len(ctx.args)!=1 or not ctx.args[0].isdigit():
        return update.message.reply_text("Usage: /delay <seconds>")
    sec = int(ctx.args[0])
    if sec<MIN_INTERVAL:
        return update.message.reply_text(f"Min {MIN_INTERVAL}s")
    chat = get_chat(update.effective_chat.id)
    chat['interval'] = sec
    update_chat(update.effective_chat.id, chat)
    update.message.reply_text(f"Interval set to {sec}s")


def stop_cmd(update: Update, ctx: CallbackContext):
    for key in ['job_auto','job_alerts']:
        job = ctx.chat_data.pop(key, None)
        if job:
            job.schedule_removal()
    update.message.reply_text("Stopped all jobs.")


def announce_cmd(update: Update, ctx: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        return update.message.reply_text("Unauthorized")
    msg = update.message.text.split(' ',1)
    if len(msg)<2:
        return update.message.reply_text("Usage: /announce <msg>")
    for cid in load_data().keys():
        ctx.bot.send_message(int(cid), msg[1])
    update.message.reply_text("Announcement sent.")


if __name__ == '__main__':
    updater = Updater(TOKEN)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('help', help_cmd))
    dp.add_handler(CommandHandler('add', add_cmd))
    dp.add_handler(CommandHandler('remove', remove_cmd))
    dp.add_handler(CommandHandler('list', list_cmd))
    dp.add_handler(CommandHandler('check', check_cmd))
    dp.add_handler(CommandHandler('auto', auto_cmd))
    dp.add_handler(CommandHandler('alerts', alerts_cmd))
    dp.add_handler(CommandHandler('delay', delay_cmd))
    dp.add_handler(CommandHandler('stop', stop_cmd))
    dp.add_handler(CommandHandler('announce', announce_cmd))
    updater.start_polling()
    updater.idle()
