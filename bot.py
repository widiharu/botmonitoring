#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Button Keyboard Version

Commands via buttons or slash:
• Add (⌂ Add Address)          – Add node
• Remove (⌂ Remove Address)    – Remove node
• List Address                 – List nodes
• Status                       – Show status now
• Auto                         – Start auto-update
• Alerts                       – Enable alerts
• Delay                        – Set auto-update interval
• Stop                         – Stop auto-updates & alerts
• Announce                     – (Admin only) Broadcast announcement

Max nodes per chat: 5
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ParseMode, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters

# Load config
load_dotenv()
TOKEN            = os.getenv("TOKEN")
API_KEY          = os.getenv("API_KEY")
CORTENSOR_API    = os.getenv("CORTENSOR_API", "https://dashboard-devnet3.cortensor.network")
ADMIN_IDS        = [int(x) for x in os.getenv("ADMIN_IDS",""
                       ).split(",") if x]
MAX_NODES        = int(os.getenv("MAX_ADDRESS_PER_CHAT", 5))
DATA_FILE        = "data.json"
DEFAULT_INTERVAL = 300
MIN_INTERVAL     = 60

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Persistence

def load_data():
    if os.path.exists(DATA_FILE): return json.load(open(DATA_FILE))
    return {}

def save_data(d): json.dump(d, open(DATA_FILE, "w"), indent=2)

def get_chat(cid):
    d = load_data()
    return d.setdefault(str(cid), {"nodes":[], "interval":DEFAULT_INTERVAL})

def update_chat(cid,data):
    d=load_data(); d[str(cid)]=data; save_data(d)

# Helpers

def shorten(addr): return addr[:6]+"..."+addr[-4:]

def age(ts):
    delta=datetime.now()-datetime.fromtimestamp(ts)
    if delta.days>0: return f"{delta.days}d {delta.seconds//3600}h ago"
    h=delta.seconds//3600; m=(delta.seconds%3600)//60
    return f"{h}h {m}m ago" if h else f"{m}m ago"

# Etherscan

def fetch_txs(addr):
    try:
        r=requests.get("https://api-sepolia.arbiscan.io/api",
                     params={"module":"account","action":"txlist",
                             "address":addr,"sort":"desc","page":1,
                             "offset":100,"apikey":API_KEY},timeout=10)
        return r.json().get("result",[])
    except: return []

def fetch_balance(addr):
    try:
        r=requests.get("https://api-sepolia.arbiscan.io/api",
            params={"module":"account","action":"balance",
                    "address":addr,"tag":"latest","apikey":API_KEY},
            timeout=10)
        return int(r.json().get("result","0"))/1e18
    except: return 0.0

# Methods
METHODS={"0xf21a494b":"Commit","0x65c815a5":"Precommit",
         "0xca6726d9":"Prepare","0x198e2b8a":"Create"}
PING="0x5c36b186"

def last_successful(txs):
    for tx in txs:
        inp=tx.get("input","")
        if inp.startswith(PING) or tx.get("isError")!="0": continue
        m=inp[:10]
        if m in METHODS: return METHODS[m],int(tx["timeStamp"])
    return None,None

# Build report
def build_report(n,i):
    addr=n["wallet"]; label=n.get("label",f"Node {i}")
    txs=fetch_txs(addr); bal=fetch_balance(addr)
    last_tx=int(txs[0]["timeStamp"]) if txs else 0
    status="🟢 Online" if datetime.now()-datetime.fromtimestamp(last_tx)<timedelta(minutes=5) else "🔴 Offline"
    last_act=age(last_tx) if txs else "N/A"
    groups=[txs[j*5:(j+1)*5] for j in range(5)]
    health=" ".join("🟩" if g and all(t.get("isError")=="0" for t in g) else "⬜" if not g else "🟥" for g in groups)
    last25=txs[:25]; stalled=bool(last25) and all(t.get("input",""
                              ).startswith(PING) for t in last25)
    name,ts=last_successful(txs); note=f"(last successful {name} {age(ts)})" if name else ""
    stall_txt="🚨 Stalled "+note if stalled else "✅ Normal"
    return (f"🔑 {shorten(addr)} ({label})\n"
            f"💰 Balance: {bal:.4f} ETH | {status}\n"
            f"⏱️ Last Activity: {last_act}\n"
            f"🩺 Health: {health}\n"
            f"⚠️ Stall: {stall_txt}\n"
            f"🔗 https://sepolia.arbiscan.io/address/{addr}\n"
            f"📈 {CORTENSOR_API}/stats/node/{addr}\n")

# Keyboard
MENU=ReplyKeyboardMarkup([
    ["/add","/remove"],["/list","/status"],
    ["/auto","/alerts"],["/delay","/stop"],
    ["/announce"]
],resize_keyboard=True)

# Handlers

def start(update:Update,ctx:CallbackContext):
    update.message.reply_text("Welcome!",reply_markup=MENU)

def help_cmd(update:Update,ctx:CallbackContext):
    update.message.reply_text("Use buttons or slash commands.",reply_markup=MENU)

# CRUD

def add(update:Update,ctx:CallbackContext):
    parts=update.message.text.split(maxsplit=1)
    if len(parts)<2: return update.message.reply_text("/add <wallet>[,label]",reply_markup=MENU)
    wlbl=parts[1].split(",",1)
    wallet=wlbl[0].strip(); label=wlbl[1].strip() if len(wlbl)>1 else None
    chat=get_chat(update.effective_chat.id)
    if len(chat['nodes'])>=MAX_NODES: return update.message.reply_text(f"Max {MAX_NODES} nodes.",reply_markup=MENU)
    chat['nodes'].append({'wallet':wallet,'label':label});update_chat(update.effective_chat.id,chat)
    update.message.reply_text(f"Added {label or shorten(wallet)}",reply_markup=MENU)


def remove(update:Update,ctx:CallbackContext):
    if not ctx.args: return update.message.reply_text("/remove <wallet>",reply_markup=MENU)
    wallet=ctx.args[0];chat=get_chat(update.effective_chat.id)
    chat['nodes']=[n for n in chat['nodes'] if n['wallet']!=wallet];update_chat(update.effective_chat.id,chat)
    update.message.reply_text(f"Removed {wallet}",reply_markup=MENU)


def list_cmd(update:Update,ctx:CallbackContext):
    nodes=get_chat(update.effective_chat.id)['nodes']
    if not nodes: return update.message.reply_text("No nodes.",reply_markup=MENU)
    text="\n".join(f"- {n.get('label') or shorten(n['wallet'])}: {n['wallet']}" for n in nodes)
    update.message.reply_text(text,reply_markup=MENU)


def status(update:Update,ctx:CallbackContext):
    nodes=get_chat(update.effective_chat.id)['nodes']
    if not nodes: return update.message.reply_text("No nodes.",reply_markup=MENU)
    msg="Auto Update\n\n"
    for i,n in enumerate(nodes,1): msg+=build_report(n,i)+"\n"
    for chunk in [msg[i:i+4000] for i in range(0,len(msg),4000)]:
        update.message.reply_text(chunk,parse_mode=ParseMode.MARKDOWN,reply_markup=MENU)


def auto(update:Update,ctx:CallbackContext):
    chat=get_chat(update.effective_chat.id)
    if 'job_auto' in ctx.chat_data: return update.message.reply_text("Auto on",reply_markup=MENU)
    job=ctx.job_queue.run_repeating(lambda _:status(update,ctx),interval=chat['interval'],first=0)
    ctx.chat_data['job_auto']=job;update.message.reply_text("Auto started",reply_markup=MENU)


def alerts(update:Update,ctx:CallbackContext):
    if 'job_alert' in ctx.chat_data: return update.message.reply_text("Alerts on",reply_markup=MENU)
    job=ctx.job_queue.run_repeating(lambda _:status(update,ctx),interval=900,first=0)
    ctx.chat_data['job_alert']=job;update.message.reply_text("Alerts on",reply_markup=MENU)


def delay(update:Update,ctx:CallbackContext):
    if not ctx.args or not ctx.args[0].isdigit(): return update.message.reply_text("/delay <sec>",reply_markup=MENU)
    sec=int(ctx.args[0])
    if sec<MIN_INTERVAL: return update.message.reply_text(f"Min {MIN_INTERVAL}s",reply_markup=MENU)
    chat=get_chat(update.effective_chat.id);chat['interval']=sec;update_chat(update.effective_chat.id,chat)
    update.message.reply_text(f"Interval {sec}s",reply_markup=MENU)


def stop(update:Update,ctx:CallbackContext):
    for k in ('job_auto','job_alert'):
        j=ctx.chat_data.pop(k,None)
        if j:j.schedule_removal()
    update.message.reply_text("Stopped all",reply_markup=MENU)


def announce(update:Update,ctx:CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return update.message.reply_text("Unauthorized.",reply_markup=MENU)
    msg=update.message.text.split(' ',1)[1] if ' ' in update.message.text else ''
    for cid in load_data(): ctx.bot.send_message(int(cid),msg)
    update.message.reply_text("Announcement sent",reply_markup=MENU)

# Main

def main():
    updater=Updater(TOKEN)
    dp=updater.dispatcher
    dp.add_handler(CommandHandler("start",start))
    dp.add_handler(CommandHandler("help",help_cmd))
    dp.add_handler(CommandHandler("add",add))
    dp.add_handler(CommandHandler("remove",remove))
    dp.add_handler(CommandHandler("list",list_cmd))
    dp.add_handler(CommandHandler("status",status))
    dp.add_handler(CommandHandler("auto",auto))
    dp.add_handler(CommandHandler("alerts",alerts))
    dp.add_handler(CommandHandler("delay",delay))
    dp.add_handler(CommandHandler("stop",stop))
    dp.add_handler(CommandHandler("announce",announce))
    updater.start_polling()
    logger.info("Bot running...")
    updater.idle()

if __name__=="__main__": main()