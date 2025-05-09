#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Reply Keyboard Version

Features:
• Add Address (with optional label, format: <wallet_address>,<label>)
• Remove Address
• Check Status
• Auto Update
• Enable Alerts
• Set Delay (custom auto update interval per chat)
• Stop
• Announce (admin only)

Maximum nodes per chat is now controlled by MAX_ADDRESS_PER_CHAT in .env
"""

import logging
import requests
import json
import os
import time
from datetime import datetime, timedelta, timezone
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, ConversationHandler, CallbackContext
from dotenv import load_dotenv

load_dotenv()

# -------------------- CONFIGURATION --------------------
TOKEN                  = os.getenv("TOKEN")
API_KEY                = os.getenv("API_KEY")
DEFAULT_UPDATE_INTERVAL= 300  # seconds
CORTENSOR_API          = os.getenv("CORTENSOR_API")
ADMIN_IDS              = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()]
MAX_ADDRESS_PER_CHAT   = int(os.getenv("MAX_ADDRESS_PER_CHAT", 5))
DATA_FILE              = "data.json"
MIN_AUTO_UPDATE_INTERVAL = 60  # seconds

# -------------------- INITIALIZATION --------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

# -------------------- STATES --------------------
ADD_ADDR, RM_ADDR, ANNOUNCE, SET_DELAY = range(1,5)

# -------------------- DATA STORAGE --------------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            return json.load(open(DATA_FILE))
        except Exception as e:
            logger.error(f"load_data error: {e}")
    return {}

def save_data(d):
    try:
        json.dump(d, open(DATA_FILE,"w"))
    except Exception as e:
        logger.error(f"save_data error: {e}")

def get_chat(chat_id):
    d = load_data()
    return d.get(str(chat_id), {"addresses":[], "auto_update_interval":DEFAULT_UPDATE_INTERVAL})

def set_chat(chat_id, info):
    d = load_data()
    d[str(chat_id)] = info
    save_data(d)

def get_addresses(chat_id):
    return get_chat(chat_id)["addresses"]

def set_addresses(chat_id, addrs):
    info = get_chat(chat_id)
    info["addresses"] = addrs
    set_chat(chat_id, info)

def get_interval(chat_id):
    return get_chat(chat_id)["auto_update_interval"]

def set_interval(chat_id, iv):
    info = get_chat(chat_id)
    info["auto_update_interval"] = iv
    set_chat(chat_id, info)

# -------------------- HELPERS --------------------
def shorten(a): return a[:6]+"..."+a[-4:] if len(a)>10 else a
def now_wib(): return datetime.now(WIB)
def fmt_time(t): return t.strftime("%Y-%m-%d %H:%M:%S WIB")
def age(ts):
    d = now_wib() - datetime.fromtimestamp(ts, WIB)
    s = int(d.total_seconds())
    if s<60: return f"{s} secs ago"
    m = s//60
    if m<60: return f"{m} mins ago"
    return f"{m//60} hours ago"

def main_menu(chat_id):
    kb = [
        ["Add Address","Remove Address"],
        ["Check Status","Auto Update"],
        ["Enable Alerts","Set Delay"],
        ["Stop"]
    ]
    if chat_id in ADMIN_IDS: kb.append(["Announce"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=False)

def send_long(bot, cid, txt):
    maxl=4096
    if len(txt)<=maxl:
        bot.send_message(cid,txt,parse_mode="Markdown")
    else:
        parts=txt.split("\n")
        buf=""
        for p in parts:
            if len(buf)+len(p)+1>maxl:
                bot.send_message(cid,buf,parse_mode="Markdown")
                buf=p
            else:
                buf+=( "\n"+p if buf else p )
        if buf: bot.send_message(cid,buf,parse_mode="Markdown")

# -------------------- API & RATE LIMIT --------------------
def delay_for(n): return max(3.0, (2*n)/0.5/(2*n-1))  # simple base 3s

def fetch_balance(addr):
    for i in range(3):
        try:
            r=requests.get("https://api-sepolia.arbiscan.io/api", params={
                "module":"account","action":"balance",
                "address":addr,"tag":"latest","apikey":API_KEY},timeout=10
            ).json().get("result","0")
            return int(r)/1e18
        except:
            time.sleep(3*(i+1))
    return 0.0

def fetch_txs(addr):
    for i in range(3):
        try:
            res=requests.get("https://api-sepolia.arbiscan.io/api",params={
                "module":"account","action":"txlist","address":addr,
                "sort":"desc","page":1,"offset":100,"apikey":API_KEY},timeout=10
            ).json().get("result",[])
            if isinstance(res,list): return res
        except:
            time.sleep(3*(i+1))
    return []

def fetch_stats(addr):
    try:
        return requests.get(f"{CORTENSOR_API}/stats/node/{addr}", timeout=15).json()
    except:
        return {}

allowed_methods = {
    "0xf21a494b":"Commit",
    "0x65c815a5":"Precommit",
    "0xca6726d9":"Prepare",
    "0x198e2b8a":"Create"
}

def last_allowed(txs):
    for tx in txs:
        m=tx.get("input","").lower()
        if m.startswith("0x5c36b186"): continue
        if tx.get("isError")!="0": continue
        for k,v in allowed_methods.items():
            if m.startswith(k): return v,int(tx["timeStamp"])
        if "create" in m: return "Create",int(tx["timeStamp"])
    return None

# -------------------- JOBS --------------------
def auto_update(ctx):
    cid=ctx.job.context["chat_id"]
    addrs=get_addresses(cid)[:MAX_ADDRESS_PER_CHAT]
    if not addrs:
        ctx.bot.send_message(cid,"ℹ️ No addresses found! Use Add Address.")
        return
    lines=[]
    for item in addrs:
        a,label = (item["address"],item.get("label","")) if isinstance(item,dict) else (item,"")
        disp=f"🔑 {shorten(a)}"+(f" ({label})" if label else "")
        bal=fetch_balance(a)
        txs=fetch_txs(a)
        if txs:
            lt=int(txs[0]["timeStamp"]); ta=age(lt)
            st = "🟢 Online" if now_wib()-datetime.fromtimestamp(lt,WIB)<=timedelta(minutes=5) else "🔴 Offline"
            last25=txs[:25]
            stalled = all(tx.get("input","").lower().startswith("0x5c36b186") for tx in last25)
            if stalled:
                ls=last_allowed(txs)
                extra = f" (last successful {ls[0]} was {age(ls[1])})" if ls else ""
                note=f"Transaction: last successful {ls[0]} was {age(ls[1])}" if ls else "Transaction: none"
                stall_txt=f"🚨 Node Stall{extra}"
            else:
                stall_txt="✅ Normal"
                ls=last_allowed(txs)
                note=f"Transaction: last successful {ls[0]} was {age(ls[1])}" if ls else "Transaction: none"
            groups=[last25[i*5:(i+1)*5] for i in range(5)]
            health=" ".join("🟩" if all(tx.get("isError")=="0" for tx in g) else "🟥" if g else "⬜" for g in groups)
        else:
            ta="N/A"; st="🔴 Offline"; health="No tx"; stall_txt="N/A"; note="Transaction: N/A"
        lines.append(
            f"*{disp}*\n"
            f"💰 `{bal:.4f} ETH` | {st}\n"
            f"⏱️ Last Activity: `{ta}`\n"
            f"🩺 Health: {health}\n"
            f"⚠️ Stall: {stall_txt}\n"
            f"{note}\n"
            f"[🔗 Arbiscan](https://sepolia.arbiscan.io/address/{a}) | [📈 Dashboard]({CORTENSOR_API}/stats/node/{a})"
        )
    out="*Auto Update*\n\n"+ "\n\n".join(lines) + f"\n\n_Last update: {fmt_time(now_wib())}_"
    send_long(ctx.bot,cid,out)

def alert_check(ctx):
    cid=ctx.job.context["chat_id"]
    for item in get_addresses(cid)[:MAX_ADDRESS_PER_CHAT]:
        a,label = (item["address"],item.get("label","")) if isinstance(item,dict) else (item,"")
        txs=fetch_txs(a)
        if txs:
            lt=int(txs[0]["timeStamp"])
            if now_wib()-datetime.fromtimestamp(lt,WIB)>timedelta(minutes=15):
                ctx.bot.send_message(cid,f"🚨 *Alert {shorten(a)}*: no tx in last 15m",parse_mode="Markdown")
                continue
            ls=last_allowed(txs)
            if ls and now_wib()-datetime.fromtimestamp(ls[1],WIB)>timedelta(minutes=15):
                ctx.bot.send_message(cid,f"🚨 *Alert {shorten(a)}*: node stall, last {ls[0]} at {age(ls[1])}",parse_mode="Markdown")
        else:
            ctx.bot.send_message(cid,f"🚨 *Alert {shorten(a)}*: no tx at all",parse_mode="Markdown")

# -------------------- CONVERSATIONS --------------------
def cancel(update,ctx):
    update.message.reply_text("Cancelled.",reply_markup=main_menu(update.effective_chat.id))
    return ConversationHandler.END

def set_delay_start(update,ctx):
    update.message.reply_text("Enter auto‐update interval in seconds (min 60):",reply_markup=ReplyKeyboardRemove())
    return SET_DELAY

def set_delay_recv(update,ctx):
    cid=update.effective_chat.id
    try:
        v=float(update.message.text)
        if v<MIN_AUTO_UPDATE_INTERVAL:
            raise ValueError
        set_interval(cid,v)
        update.message.reply_text(f"Interval set to {v}s",reply_markup=main_menu(cid))
        return ConversationHandler.END
    except:
        update.message.reply_text("Invalid; enter number ≥60 or /cancel")
        return SET_DELAY

def add_addr_start(update,ctx):
    update.message.reply_text("Send `<wallet>,<label>` or just `<wallet>`:",reply_markup=ReplyKeyboardRemove())
    return ADD_ADDR

def add_addr_recv(update,ctx):
    cid=update.effective_chat.id
    txt=update.message.text.strip().split(",",1)
    w=txt[0].lower(); lbl=txt[1] if len(txt)>1 else ""
    if not w.startswith("0x") or len(w)!=42:
        update.message.reply_text("Bad address. Try again or /cancel")
        return ADD_ADDR
    arr=get_addresses(cid)
    if any((i if isinstance(i,str) else i["address"])==w for i in arr):
        update.message.reply_text("Already added",reply_markup=main_menu(cid))
        return ConversationHandler.END
    if len(arr)>=MAX_ADDRESS_PER_CHAT:
        update.message.reply_text("Max nodes reached",reply_markup=main_menu(cid))
        return ConversationHandler.END
    arr.append({"address":w,"label":lbl})
    set_addresses(cid,arr)
    update.message.reply_text(f"Added {shorten(w)}",reply_markup=main_menu(cid))
    return ConversationHandler.END

def rm_addr_start(update,ctx):
    cid=update.effective_chat.id
    arr=get_addresses(cid)
    if not arr:
        update.message.reply_text("None to remove",reply_markup=main_menu(cid))
        return ConversationHandler.END
    kb=[[ (i if isinstance(i,str) else i["address"])+ (f" ({i['label']})" if isinstance(i,dict) and i['label'] else "") ] for i in arr]
    kb.append(["Cancel"])
    update.message.reply_text("Choose to remove:",reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True,one_time_keyboard=True))
    return RM_ADDR

def rm_addr_recv(update,ctx):
    cid=update.effective_chat.id; ch=update.message.text
    if ch=="Cancel":
        return cancel(update,ctx)
    arr=[]
    found=False
    for i in get_addresses(cid):
        w,l=(i,i) if isinstance(i,str) else (i["address"],i["label"])
        disp=w+(f" ({l})" if l else "")
        if disp==ch:
            found=True
        else:
            arr.append(i)
    if not found:
        update.message.reply_text("Not found",reply_markup=main_menu(cid))
        return ConversationHandler.END
    set_addresses(cid,arr)
    update.message.reply_text("Removed",reply_markup=main_menu(cid))
    return ConversationHandler.END

def announce_start(update,ctx):
    cid=update.effective_chat.id
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("No auth",reply_markup=main_menu(cid))
        return ConversationHandler.END
    update.message.reply_text("Send announcement:",reply_markup=ReplyKeyboardRemove())
    return ANNOUNCE

def announce_recv(update,ctx):
    txt=update.message.text; cnt=0
    for k in load_data().keys():
        try: ctx.bot.send_message(chat_id=int(k),text=txt); cnt+=1
        except: pass
    update.message.reply_text(f"Sent to {cnt} chats",reply_markup=main_menu(update.effective_chat.id))
    return ConversationHandler.END

# -------------------- COMMAND HANDLERS --------------------
def menu_stop(update,ctx):
    cid=update.effective_chat.id; rm=0
    for n in (f"auto_update_{cid}",f"alert_{cid}"):
        for j in ctx.job_queue.get_jobs_by_name(n):
            j.schedule_removal(); rm+=1
    msg="Stopped" if rm else "None running"
    update.message.reply_text(f"{msg}",reply_markup=main_menu(cid))

def menu_auto(update,ctx):
    cid=update.effective_chat.id
    if not get_addresses(cid):
        update.message.reply_text("No nodes added",reply_markup=main_menu(cid)); return
    if ctx.job_queue.get_jobs_by_name(f"auto_update_{cid}"):
        update.message.reply_text("Already running",reply_markup=main_menu(cid)); return
    ctx.job_queue.run_repeating(auto_update, interval=get_interval(cid), context={"chat_id":cid}, name=f"auto_update_{cid}")
    update.message.reply_text(f"Auto-update every {get_interval(cid)}s",reply_markup=main_menu(cid))

def menu_alerts(update,ctx):
    cid=update.effective_chat.id
    if not get_addresses(cid):
        update.message.reply_text("No nodes added",reply_markup=main_menu(cid)); return
    if ctx.job_queue.get_jobs_by_name(f"alert_{cid}"):
        update.message.reply_text("Alerts on",reply_markup=main_menu(cid)); return
    ctx.job_queue.run_repeating(alert_check, interval=900, context={"chat_id":cid}, name=f"alert_{cid}")
    update.message.reply_text("Alerts enabled",reply_markup=main_menu(cid))

def menu_check(update,ctx):
    cid=update.effective_chat.id; lines=[]
    for item in get_addresses(cid)[:MAX_ADDRESS_PER_CHAT]:
        a,l=(item,item) if isinstance(item,str) else (item["address"],item["label"])
        bal=fetch_balance(a)
        txs=fetch_txs(a)
        if txs:
            lt=int(txs[0]["timeStamp"]); ta=age(lt)
            st="🟢 Online" if now_wib()-datetime.fromtimestamp(lt,WIB)<=timedelta(minutes=5) else "🔴 Offline"
            last25=txs[:25]
            stalled=all(tx.get("input","").lower().startswith("0x5c36b186") for tx in last25)
            if stalled:
                ls=last_allowed(txs)
                ex=f" (last successful {ls[0]} {age(ls[1])})" if ls else ""
                note=f"Transaction: last {ls[0]} {age(ls[1])}" if ls else "Transaction: none"
            else:
                ex=""; ls=last_allowed(txs)
                note=f"Transaction: last {ls[0]} {age(ls[1])}" if ls else "Transaction: none"
            health=" ".join("🟩" if all(tx.get("isError")=="0" for tx in g) else "🟥" if g else "⬜" for g in [last25[i*5:(i+1)*5] for i in range(5)])
            stall_txt= "🚨 Node Stall"+ex if stalled else "✅ Normal"
        else:
            ta="N/A"; st="🔴 Offline"; health="No tx"; stall_txt="N/A"; note="Transaction: N/A"
        disp=f"🔑 {shorten(a)}"+(f" ({l})" if l else "")
        lines.append(
            f"*{disp}*\n"
            f"💰 `{bal:.4f} ETH` | {st}\n"
            f"⏱️ Last Activity: `{ta}`\n"
            f"🩺 Health: {health}\n"
            f"⚠️ Stall: {stall_txt}\n"
            f"{note}\n"
            f"[🔗 Arbiscan](https://sepolia.arbiscan.io/address/{a}) | [📈 Dashboard]({CORTENSOR_API}/stats/node/{a})"
        )
    out="*Check Status*\n\n"+ "\n\n".join(lines) + f"\n\n_Last update: {fmt_time(now_wib())}_"
    send_long(ctx.bot,cid,out)

def start_cmd(update,ctx):
    cid=update.effective_chat.id
    update.message.reply_text("👋 Welcome! Select:",reply_markup=main_menu(cid))

def error_handler(update,ctx):
    logger.error("Error:",exc_info=ctx.error)
    for adm in ADMIN_IDS:
        try: ctx.bot.send_message(adm,f"⚠️ {ctx.error}")
        except: pass

def main():
    up=Updater(TOKEN); dp=up.dispatcher
    dp.add_handler(CommandHandler("start",start_cmd))
    dp.add_handler(CommandHandler("stop",menu_stop))
    dp.add_handler(MessageHandler(Filters.regex("^Stop$"),menu_stop))
    dp.add_handler(CommandHandler("auto_update",menu_auto))
    dp.add_handler(MessageHandler(Filters.regex("^Auto Update$"),menu_auto))
    dp.add_handler(CommandHandler("enable_alerts",menu_alerts))
    dp.add_handler(MessageHandler(Filters.regex("^Enable Alerts$"),menu_alerts))
    dp.add_handler(CommandHandler("check_status",menu_check))
    dp.add_handler(MessageHandler(Filters.regex("^Check Status$"),menu_check))
    dp.add_handler(CommandHandler("set_delay",set_delay_start))
    dp.add_handler(MessageHandler(Filters.regex("^Set Delay$"),set_delay_start))
    dp.add_error_handler(error_handler)

    convs=[
        (ADD_ADDR,    MessageHandler(Filters.text&~Filters.command,add_addr_recv)),
        (RM_ADDR,     MessageHandler(Filters.text&~Filters.command,rm_addr_recv)),
        (SET_DELAY,   MessageHandler(Filters.text&~Filters.command,set_delay_recv)),
        (ANNOUNCE,    MessageHandler(Filters.text&~Filters.command,announce_recv)),
    ]
    dp.add_handler(ConversationHandler([MessageHandler(Filters.regex("^Add Address$"),add_addr_start)],{ADD_ADDR:convs[0]},{CommandHandler("cancel",cancel)}))
    dp.add_handler(ConversationHandler([MessageHandler(Filters.regex("^Remove Address$"),rm_addr_start)],{RM_ADDR:convs[1]},{CommandHandler("cancel",cancel)}))
    dp.add_handler(ConversationHandler([MessageHandler(Filters.regex("^Set Delay$"),set_delay_start)],{SET_DELAY:convs[2]},{CommandHandler("cancel",cancel)}))
    dp.add_handler(ConversationHandler([MessageHandler(Filters.regex("^Announce$"),announce_start)],{ANNOUNCE:convs[3]},{CommandHandler("cancel",cancel)}))

    up.start_polling()
    logger.info("Bot running 🚀")
    up.idle()

if __name__=="__main__":
    main()
