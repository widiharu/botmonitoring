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

Maximum nodes per chat: 25
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
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
DEFAULT_UPDATE_INTERVAL = 300  # Default auto update interval (in seconds)
CORTENSOR_API = os.getenv("CORTENSOR_API", "https://dashboard-devnet4.cortensor.network")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATA_FILE = "data.json"
MIN_AUTO_UPDATE_INTERVAL = 60  # Minimum auto update interval (in seconds)

# -------------------- INITIALIZATION --------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))

# -------------------- CONVERSATION STATES --------------------
ADD_ADDRESS, REMOVE_ADDRESS, ANNOUNCE, SET_DELAY = range(1, 5)

# -------------------- DATA STORAGE FUNCTIONS --------------------
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    return {}

def save_data(data: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def get_chat_data(chat_id: int) -> dict:
    data = load_data()
    return data.get(str(chat_id), {"addresses": [], "auto_update_interval": DEFAULT_UPDATE_INTERVAL})

def update_chat_data(chat_id: int, chat_data: dict):
    data = load_data()
    data[str(chat_id)] = chat_data
    save_data(data)

def get_addresses_for_chat(chat_id: int) -> list:
    return get_chat_data(chat_id).get("addresses", [])

def update_addresses_for_chat(chat_id: int, addresses: list):
    chat_data = get_chat_data(chat_id)
    chat_data["addresses"] = addresses
    update_chat_data(chat_id, chat_data)

def get_auto_update_interval(chat_id: int) -> float:
    return get_chat_data(chat_id).get("auto_update_interval", DEFAULT_UPDATE_INTERVAL)

def update_auto_update_interval(chat_id: int, interval: float):
    chat_data = get_chat_data(chat_id)
    chat_data["auto_update_interval"] = interval
    update_chat_data(chat_id, chat_data)

# -------------------- UTILITY FUNCTIONS --------------------
def parse_address_item(item):
    if isinstance(item, dict):
        return item.get("address"), item.get("label", "")
    return item, ""

def shorten_address(address: str) -> str:
    return address[:6] + "..." + address[-4:] if len(address) > 10 else address

def get_wib_time() -> datetime:
    return datetime.now(WIB)

def format_time(time_obj: datetime) -> str:
    return time_obj.strftime('%Y-%m-%d %H:%M:%S WIB')

def get_age(timestamp: int) -> str:
    diff = datetime.now(WIB) - datetime.fromtimestamp(timestamp, WIB)
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return f"{seconds} secs ago"
    minutes = seconds // 60
    return f"{minutes} mins ago" if minutes < 60 else f"{minutes//60} hours ago"

def main_menu_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        ["Add Address", "Remove Address"],
        ["Check Status", "Auto Update"],
        ["Enable Alerts", "Set Delay"],
        ["Stop"]
    ]
    if chat_id in ADMIN_IDS:
        keyboard.append(["Announce"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

# -------------------- MESSAGE SENDING HELPER --------------------
def send_long_message(bot, chat_id, text, parse_mode="Markdown"):
    max_length = 4096
    if len(text) <= max_length:
        bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    else:
        parts = text.split("\n")
        current_msg = ""
        for part in parts:
            if len(current_msg) + len(part) + 1 > max_length:
                bot.send_message(chat_id=chat_id, text=current_msg, parse_mode=parse_mode)
                current_msg = part
            else:
                current_msg += ("\n" if current_msg else "") + part
        if current_msg:
            bot.send_message(chat_id=chat_id, text=current_msg, parse_mode=parse_mode)

# -------------------- API FUNCTIONS --------------------
def get_dynamic_delay(num_addresses: int) -> float:
    base_delay = 3.0  # Base delay set to 3 seconds to reduce rate limit errors.
    total_calls = 2 * num_addresses  # Two API calls per address.
    if total_calls <= 0.5:
        return base_delay
    required_total_time = total_calls / 0.5
    intervals = total_calls - 1
    dynamic_delay = required_total_time / intervals
    return max(dynamic_delay, base_delay)

def safe_fetch_balance(address: str, delay: float) -> float:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            params = {"module": "account", "action": "balance", "address": address,
                      "tag": "latest", "apikey": API_KEY}
            response = requests.get("https://api-sepolia.arbiscan.io/api", params=params, timeout=10)
            json_resp = response.json()
            result_str = json_resp.get("result", "")
            try:
                balance_int = int(result_str)
                return balance_int / 10**18
            except ValueError:
                if "Max calls per sec rate limit" in result_str:
                    logger.error(f"Rate limit reached for {address}. Retrying (attempt {attempt+1})...")
                    time.sleep(delay * (attempt+1) * 2)
                    continue
                else:
                    logger.error(f"Balance error for {address}: {result_str}")
                    return 0.0
        except Exception as e:
            logger.error(f"Exception fetching balance for {address}: {e}")
        time.sleep(delay * (attempt+1))
    return 0.0

def safe_fetch_transactions(address: str, delay: float) -> list:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            params = {"module": "account", "action": "txlist", "address": address,
                      "sort": "desc", "page": 1, "offset": 100, "apikey": API_KEY}
            response = requests.get("https://api-sepolia.arbiscan.io/api", params=params, timeout=10)
            json_resp = response.json()
            result = json_resp.get("result", [])
            if isinstance(result, list) and result and isinstance(result[0], dict):
                return result
            else:
                if isinstance(result, str) and "Max calls per sec rate limit" in result:
                    logger.error(f"Rate limit reached for transactions of {address}. Retrying (attempt {attempt+1})...")
                    time.sleep(delay * (attempt+1) * 2)
                    continue
                else:
                    logger.error(f"Unexpected transactions format for {address}: {result}")
                    return []
        except Exception as e:
            logger.error(f"Exception fetching transactions for {address}: {e}")
        time.sleep(delay * (attempt+1))
    return []

# -------------------- STALL DURATION HELPER --------------------
def get_last_allowed_transaction(txs: list):
    """
    Scan the transactions (from newest to oldest) for the most recent successful transaction 
    (isError == "0") whose method matches one of:
      • 0xf21a494b → Commit
      • 0x65c815a5 → Precommit
      • 0xca6726d9 → Prepare
      • 0x198e2b8a → Create
    """
    allowed = {
        "0xf21a494b": "Commit",
        "0x65c815a5": "Precommit",
        "0xca6726d9": "Prepare",
        "0x198e2b8a": "Create"
    }
    for tx in txs:
        method = tx.get('input', '').lower()
        if method.startswith("0x5c36b186"):  # Skip PING
            continue
        if tx.get("isError") != "0":
            continue
        for key, label in allowed.items():
            if method.startswith(key):
                return (label, int(tx['timeStamp']))
        if "create" in method:
            return ("Create", int(tx['timeStamp']))
    return None

# -------------------- JOB FUNCTIONS --------------------
def auto_update(context: CallbackContext):
    chat_id = context.job.context['chat_id']
    addresses = get_addresses_for_chat(chat_id)[:25]
    if not addresses:
        context.bot.send_message(chat_id=chat_id, text="ℹ️ No addresses found! Please add one using 'Add Address'.")
        return
    output_lines = []
    for item in addresses:
        wallet, label = parse_address_item(item)
        addr_display = f"🔑 {shorten_address(wallet)}" + (f" ({label})" if label else "")
        balance = safe_fetch_balance(wallet, delay=2.0)
        txs = safe_fetch_transactions(wallet, delay=2.0)
        if txs:
            last_tx_time = int(txs[0]['timeStamp'])
            time_diff = datetime.now(WIB) - datetime.fromtimestamp(last_tx_time, WIB)
            status = "🟢 Online" if time_diff <= timedelta(minutes=5) else "🔴 Offline"
            last_activity = get_age(last_tx_time)
            latest_25 = txs[:25]
            if latest_25 and all(tx.get('input', '').lower().startswith("0x5c36b186") for tx in latest_25):
                stall_status = "🚨 Node Stall"
                last_allowed = get_last_allowed_transaction(txs)
                if last_allowed:
                    method_label, ts = last_allowed
                    stall_extra = f" (last successful {method_label} transaction was {get_age(ts)})"
                    transaction_note = f"Transaction: (last successful {method_label} transaction was {get_age(ts)})"
                else:
                    stall_extra = " (stale duration N/A)"
                    transaction_note = "Transaction: None found."
            else:
                stall_status = "✅ Normal"
                stall_extra = ""
                last_allowed = get_last_allowed_transaction(txs)
                if last_allowed:
                    method_label, ts = last_allowed
                    transaction_note = f"Transaction: (last successful {method_label} transaction was {get_age(ts)})"
                else:
                    transaction_note = "Transaction: None found."
            groups = [latest_25[i*5:(i+1)*5] for i in range(5)]
            health_list = [("🟩" if all(tx.get('isError') == '0' for tx in group) else "🟥") if group else "⬜" for group in groups]
            health_status = " ".join(health_list)
        else:
            status = "🔴 Offline"
            last_activity = "N/A"
            health_status = "No transactions"
            stall_status = "N/A"
            stall_extra = ""
            transaction_note = "Transaction: N/A"
        output_lines.append(
            f"*{addr_display}*\n"
            f"💰 Balance: `{balance:.4f} ETH` | Status: {status}\n"
            f"⏱️ Last Activity: `{last_activity}`\n"
            f"🩺 Health: {health_status}\n"
            f"⚠️ Stall: {stall_status}{stall_extra}\n"
            f"{transaction_note}\n"
            f"[🔗 Arbiscan](https://sepolia.arbiscan.io/address/{wallet}) | [📈 Dashboard]({CORTENSOR_API}/stats/node/{wallet})"
        )
    final_output = "*Auto Update*\n\n" + "\n\n".join(output_lines) + f"\n\n_Last update: {format_time(get_wib_time())}_"
    send_long_message(context.bot, chat_id, final_output, parse_mode="Markdown")

def alert_check(context: CallbackContext):
    chat_id = context.job.context['chat_id']
    addresses = get_addresses_for_chat(chat_id)[:25]
    for item in addresses:
        wallet, label = parse_address_item(item)
        txs = safe_fetch_transactions(wallet, delay=2.0)
        if txs:
            last_tx_time = int(txs[0]['timeStamp'])
            time_diff = datetime.now(WIB) - datetime.fromtimestamp(last_tx_time, WIB)
            if time_diff > timedelta(minutes=15):
                msg = f"🚨 *Alert for {shorten_address(wallet)}" + (f" ({label})" if label else "") + "*:\n⏱️ No transactions in the last 15 minutes."
                context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                continue
            last_allowed = get_last_allowed_transaction(txs)
            if last_allowed:
                method_label, ts = last_allowed
                if datetime.now(WIB) - datetime.fromtimestamp(ts, WIB) > timedelta(minutes=15):
                    msg = f"🚨 *Alert for {shorten_address(wallet)}" + (f" ({label})" if label else "") + "*:\n⚠️ Node stall detected (last successful {method_label} transaction was {get_age(ts)})."
                    context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        else:
            context.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *Alert for {shorten_address(wallet)}" + (f" ({label})" if label else "") + "*:\n- No transactions found!\n[🔗 Arbiscan](https://sepolia.arbiscan.io/address/{wallet}) | [📈 Dashboard]({CORTENSOR_API}/stats/node/{wallet})",
                parse_mode="Markdown"
            )

# -------------------- COMMAND HANDLER FUNCTIONS --------------------
def set_delay_start(update, context):
    update.effective_message.reply_text(
        "Please enter the custom auto update interval (in seconds). (Minimum is 60 seconds)\n(Send /cancel to abort)",
        reply_markup=ReplyKeyboardRemove()
    )
    return SET_DELAY

def set_delay_receive(update, context):
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip()
    try:
        new_interval = float(text)
        if new_interval < MIN_AUTO_UPDATE_INTERVAL:
            update.effective_message.reply_text("The auto update interval must be at least 60 seconds. Try again or send /cancel.")
            return SET_DELAY
        update_auto_update_interval(chat_id, new_interval)
        update.effective_message.reply_text(f"✅ Auto update interval set to {new_interval} seconds.",
                                              reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    except ValueError:
        update.effective_message.reply_text("❌ Please enter a valid number for the auto update interval.")
        return SET_DELAY

def cancel(update, context):
    update.effective_message.reply_text("Operation cancelled.", reply_markup=main_menu_keyboard(update.effective_chat.id))
    return ConversationHandler.END

def add_address_start(update, context):
    update.effective_message.reply_text(
        "Please send the wallet address to add in the format `<wallet_address>,<label>` (label is optional).\nExample: 0xABC123...7890,My Node\n(Send /cancel to abort)",
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_ADDRESS

def add_address_receive(update, context):
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip()
    parts = [x.strip() for x in text.split(",")]
    wallet = parts[0].lower()
    label = parts[1] if len(parts) > 1 else ""
    if not wallet.startswith("0x") or len(wallet) != 42:
        update.effective_message.reply_text("❌ Invalid wallet address! It must start with '0x' and be 42 characters long. Try again or send /cancel to abort.")
        return ADD_ADDRESS
    addresses = get_addresses_for_chat(chat_id)
    if any((item.get("address") if isinstance(item, dict) else item) == wallet for item in addresses):
        update.effective_message.reply_text("⚠️ Address already exists! Returning to main menu.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    if len(addresses) >= 25:
        update.effective_message.reply_text("❌ Maximum of 25 nodes per chat reached! Returning to main menu.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    addresses.append({"address": wallet, "label": label})
    update_addresses_for_chat(chat_id, addresses)
    update.effective_message.reply_text(f"✅ Added: {shorten_address(wallet)}" + (f" ({label})" if label else ""),
                                         reply_markup=main_menu_keyboard(chat_id))
    return ConversationHandler.END

def remove_address_start(update, context):
    chat_id = update.effective_chat.id
    addresses = get_addresses_for_chat(chat_id)
    if not addresses:
        update.effective_message.reply_text("No addresses found to remove.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    keyboard = []
    for item in addresses:
        wallet, label = parse_address_item(item)
        display = f"{wallet}" + (f" ({label})" if label else "")
        keyboard.append([display])
    keyboard.append(["Cancel"])
    update.effective_message.reply_text("Select the address to remove:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
    return REMOVE_ADDRESS

def remove_address_receive(update, context):
    chat_id = update.effective_chat.id
    choice = update.effective_message.text.strip()
    if choice.lower() == "cancel":
        update.effective_message.reply_text("Operation cancelled.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    addresses = get_addresses_for_chat(chat_id)
    new_addresses = []
    found = False
    for item in addresses:
        wallet, label = parse_address_item(item)
        display = f"{wallet}" + (f" ({label})" if label else "")
        if display == choice:
            found = True
            continue
        new_addresses.append(item)
    if not found:
        update.effective_message.reply_text("❌ Address not found.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    update_addresses_for_chat(chat_id, new_addresses)
    update.effective_message.reply_text("✅ Address removed.", reply_markup=main_menu_keyboard(chat_id))
    return ConversationHandler.END

def announce_start(update, context):
    chat_id = update.effective_chat.id
    if update.effective_user.id not in ADMIN_IDS:
        update.effective_message.reply_text("❌ You are not authorized to use this command.", reply_markup=main_menu_keyboard(chat_id))
        return ConversationHandler.END
    update.effective_message.reply_text("Please send the announcement message:", reply_markup=ReplyKeyboardRemove())
    return ANNOUNCE

def announce_receive(update, context):
    message = update.effective_message.text
    data = load_data()
    if not data:
        update.effective_message.reply_text("No chats found to send the announcement.", reply_markup=main_menu_keyboard(update.effective_chat.id))
        return ConversationHandler.END
    count = 0
    for chat in data.keys():
        try:
            context.bot.send_message(chat_id=int(chat), text=message)
            count += 1
        except Exception as e:
            logger.error(f"Error sending announcement to chat {chat}: {e}")
    update.effective_message.reply_text(f"📣 Announcement sent to {count} chats.", reply_markup=main_menu_keyboard(update.effective_chat.id))
    return ConversationHandler.END

def start_command(update, context):
    chat_id = update.effective_chat.id
    update.effective_message.reply_text("👋 Welcome to the Cortensor Node Monitoring Bot!\nSelect an option from the menu below:", reply_markup=main_menu_keyboard(chat_id))

def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    error_text = f"⚠️ An error occurred: {context.error}"
    for admin_id in ADMIN_IDS:
        try:
            context.bot.send_message(chat_id=admin_id, text=error_text)
        except Exception as e:
            logger.error(f"Error sending error message to admin: {e}")
            time.sleep(1)

def menu_stop(update, context):
    chat_id = update.effective_chat.id
    removed_jobs = 0
    for job_name in (f"auto_update_{chat_id}", f"alert_{chat_id}"):
        jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()
            removed_jobs += 1
    if removed_jobs:
        update.effective_message.reply_text("✅ All jobs have been stopped.", reply_markup=main_menu_keyboard(chat_id))
    else:
        update.effective_message.reply_text("No active jobs found.", reply_markup=main_menu_keyboard(chat_id))

def menu_auto_update(update, context):
    chat_id = update.effective_chat.id
    if not get_addresses_for_chat(chat_id):
        update.effective_message.reply_text("No addresses registered! Please add one using 'Add Address'.", reply_markup=main_menu_keyboard(chat_id))
        return
    interval = get_auto_update_interval(chat_id)
    current_jobs = context.job_queue.get_jobs_by_name(f"auto_update_{chat_id}")
    if current_jobs:
        update.effective_message.reply_text("Auto update is already active.", reply_markup=main_menu_keyboard(chat_id))
        return
    context.job_queue.run_repeating(auto_update, interval=interval, context={'chat_id': chat_id}, name=f"auto_update_{chat_id}")
    update.effective_message.reply_text(f"✅ Auto update started. (Interval: {interval} seconds)\nThe bot will send node updates automatically.", reply_markup=main_menu_keyboard(chat_id))

def menu_check_status(update, context):
    chat_id = update.effective_chat.id
    addresses = get_addresses_for_chat(chat_id)
    if not addresses:
        update.effective_message.reply_text("No addresses registered! Please add one using 'Add Address'.", reply_markup=main_menu_keyboard(chat_id))
        return
    output_lines = []
    for item in addresses[:25]:
        wallet, label = parse_address_item(item)
        addr_display = f"🔑 {shorten_address(wallet)}" + (f" ({label})" if label else "")
        balance = safe_fetch_balance(wallet, delay=2.0)
        txs = safe_fetch_transactions(wallet, delay=2.0)
        if txs:
            last_tx_time = int(txs[0]['timeStamp'])
            time_diff = datetime.now(WIB) - datetime.fromtimestamp(last_tx_time, WIB)
            status = "🟢 Online" if time_diff <= timedelta(minutes=5) else "🔴 Offline"
            last_activity = get_age(last_tx_time)
            latest_25 = txs[:25]
            if latest_25 and all(tx.get('input', '').lower().startswith("0x5c36b186") for tx in latest_25):
                stall_status = "🚨 Node Stall"
                last_allowed = get_last_allowed_transaction(txs)
                if last_allowed:
                    method_label, ts = last_allowed
                    stall_extra = f" (last successful {method_label} transaction was {get_age(ts)})"
                    transaction_note = f"Transaction: (last successful {method_label} transaction was {get_age(ts)})"
                else:
                    stall_extra = " (stale duration N/A)"
                    transaction_note = "Transaction: None found."
            else:
                stall_status = "✅ Normal"
                stall_extra = ""
                last_allowed = get_last_allowed_transaction(txs)
                if last_allowed:
                    method_label, ts = last_allowed
                    transaction_note = f"Transaction: (last successful {method_label} transaction was {get_age(ts)})"
                else:
                    transaction_note = "Transaction: None found."
            groups = [latest_25[i*5:(i+1)*5] for i in range(5)]
            health_list = [("🟩" if all(tx.get('isError') == '0' for tx in group) else "🟥") if group else "⬜" for group in groups]
            health_status = " ".join(health_list)
        else:
            status = "🔴 Offline"
            last_activity = "N/A"
            health_status = "No transactions"
            stall_status = "N/A"
            stall_extra = ""
            transaction_note = "Transaction: N/A"
        output_lines.append(
            f"*{addr_display}*\n"
            f"💰 Balance: `{balance:.4f} ETH` | Status: {status}\n"
            f"⏱️ Last Activity: `{last_activity}`\n"
            f"🩺 Health: {health_status}\n"
            f"⚠️ Stall: {stall_status}{stall_extra}\n"
            f"{transaction_note}\n"
            f"[🔗 Arbiscan](https://sepolia.arbiscan.io/address/{wallet}) | [📈 Dashboard]({CORTENSOR_API}/stats/node/{wallet})"
        )
    final_output = "*Check Status*\n\n" + "\n\n".join(output_lines) + f"\n\n_Last update: {format_time(get_wib_time())}_"
    send_long_message(context.bot, chat_id, final_output, parse_mode="Markdown")

def menu_enable_alerts(update, context):
    chat_id = update.effective_chat.id
    if not get_addresses_for_chat(chat_id):
        update.effective_message.reply_text("No addresses registered! Please add one using 'Add Address'.", reply_markup=main_menu_keyboard(chat_id))
        return
    current_jobs = context.job_queue.get_jobs_by_name(f"alert_{chat_id}")
    if current_jobs:
        update.effective_message.reply_text("Alerts are already active.", reply_markup=main_menu_keyboard(chat_id))
        return
    context.job_queue.run_repeating(alert_check, interval=900, context={'chat_id': chat_id}, name=f"alert_{chat_id}")
    update.effective_message.reply_text("✅ Alerts enabled.\nThe bot will monitor your nodes and send alerts if no transactions occur for 15 minutes or if a node stall is detected.", reply_markup=main_menu_keyboard(chat_id))

def start_command(update, context):
    chat_id = update.effective_chat.id
    update.effective_message.reply_text("👋 Welcome to the Cortensor Node Monitoring Bot!\nSelect an option from the menu below:", reply_markup=main_menu_keyboard(chat_id))

def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    error_text = f"⚠️ An error occurred: {context.error}"
    for admin_id in ADMIN_IDS:
        try:
            context.bot.send_message(chat_id=admin_id, text=error_text)
        except Exception as e:
            logger.error(f"Error sending error message to admin: {e}")
            time.sleep(1)

def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    logger.info("Bot is starting...")
    dp.add_handler(CommandHandler("start", start_command))
dp.add_handler(CommandHandler("auto_update", menu_auto_update))
dp.add_handler(MessageHandler(Filters.regex("^Auto Update$"), menu_auto_update))
dp.add_handler(CommandHandler("enable_alerts", menu_enable_alerts))
dp.add_handler(MessageHandler(Filters.regex("^Enable Alerts$"), menu_enable_alerts))
dp.add_handler(CommandHandler("stop", menu_stop))
dp.add_handler(MessageHandler(Filters.regex("^Stop$"), menu_stop))
dp.add_handler(CommandHandler("check_status", menu_check_status))
dp.add_handler(MessageHandler(Filters.regex("^Check Status$"), menu_check_status))
    dp.add_handler(MessageHandler(Filters.regex("^Check Status$"), menu_check_status))
    dp.add_handler(CommandHandler("announce", announce_start))
    dp.add_handler(CommandHandler("set_delay", set_delay_start))
    dp.add_handler(MessageHandler(Filters.regex("^Set Delay$"), set_delay_start))
    dp.add_error_handler(error_handler)

    conv_add = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Add Address$"), add_address_start)],
        states={ADD_ADDRESS: [MessageHandler(Filters.text & ~Filters.command, add_address_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_add)

    conv_remove = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Remove Address$"), remove_address_start)],
        states={REMOVE_ADDRESS: [MessageHandler(Filters.text & ~Filters.command, remove_address_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_remove)

    conv_announce = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Announce$"), announce_start)],
        states={ANNOUNCE: [MessageHandler(Filters.text & ~Filters.command, announce_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_announce)

    conv_set_delay = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex("^Set Delay$"), set_delay_start)],
        states={SET_DELAY: [MessageHandler(Filters.text & ~Filters.command, set_delay_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dp.add_handler(conv_set_delay)

    updater.start_polling()
    logger.info("Bot is running... 🚀")
    updater.idle()

if __name__ == "__main__":
    main()