# bot.py

#!/usr/bin/env python3
"""
Cortensor Node Monitoring Bot – Telegram Reply Keyboard Version
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

# Load configuration from .env
load_dotenv()
TOKEN = os.getenv("TOKEN")
API_KEY = os.getenv("API_KEY")
CORTENSOR_API = os.getenv("CORTENSOR_API", "https://dashboard-devnet4.cortensor.network")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Constants
DEFAULT_UPDATE_INTERVAL = 300     # seconds
MIN_AUTO_UPDATE_INTERVAL = 60     # seconds
DATA_FILE = "data.json"
WIB = timezone(timedelta(hours=7))

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
ADD_ADDRESS, REMOVE_ADDRESS, ANNOUNCE, SET_DELAY = range(1, 5)


# ————————————— Data storage ——————————————————

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
    return data.get(str(chat_id), {
        "addresses": [],
        "auto_update_interval": DEFAULT_UPDATE_INTERVAL
    })

def update_chat_data(chat_id: int, chat_data: dict):
    data = load_data()
    data[str(chat_id)] = chat_data
    save_data(data)

def get_addresses_for_chat(chat_id: int) -> list:
    return get_chat_data(chat_id)["addresses"]

def update_addresses_for_chat(chat_id: int, addresses: list):
    chat_data = get_chat_data(chat_id)
    chat_data["addresses"] = addresses
    update_chat_data(chat_id, chat_data)

def get_auto_update_interval(chat_id: int) -> float:
    return get_chat_data(chat_id)["auto_update_interval"]

def update_auto_update_interval(chat_id: int, interval: float):
    chat_data = get_chat_data(chat_id)
    chat_data["auto_update_interval"] = interval
    update_chat_data(chat_id, chat_data)


# ————————————— Utilities ——————————————————

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
    secs = int(diff.total_seconds())
    if secs < 60:
        return f"{secs} secs ago"
    mins = secs // 60
    return f"{mins} mins ago" if mins < 60 else f"{mins//60} hours ago"

def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keys = [
        ["Add Address", "Remove Address"],
        ["Check Status", "Auto Update"],
        ["Enable Alerts", "Set Delay"],
        ["Stop"]
    ]
    if user_id in ADMIN_IDS:
        keys.append(["Announce"])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True, one_time_keyboard=False)

def send_long_message(bot, chat_id: int, text: str, parse_mode="Markdown"):
    max_len = 4096
    if len(text) <= max_len:
        bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    else:
        parts = text.split("\n")
        chunk = ""
        for line in parts:
            if len(chunk) + len(line) + 1 > max_len:
                bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
                chunk = line
            else:
                chunk = chunk + "\n" + line if chunk else line
        if chunk:
            bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)


# ————————————— API calls ——————————————————

def safe_fetch_balance(address: str, delay: float) -> float:
    retries = 3
    url = "https://api-sepolia.arbiscan.io/api"
    for i in range(retries):
        try:
            resp = requests.get(url, params={
                "module":"account",
                "action":"balance",
                "address":address,
                "tag":"latest",
                "apikey":API_KEY
            }, timeout=10).json()
            res = resp.get("result","")
            bal = int(res) / 1e18
            return bal
        except ValueError:
            # rate limit or other error
            time.sleep(delay * (i+1))
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            time.sleep(delay * (i+1))
    return 0.0

def safe_fetch_transactions(address: str, delay: float) -> list:
    retries = 3
    url = "https://api-sepolia.arbiscan.io/api"
    for i in range(retries):
        try:
            resp = requests.get(url, params={
                "module":"account",
                "action":"txlist",
                "address":address,
                "sort":"desc",
                "page":1,
                "offset":100,
                "apikey":API_KEY
            }, timeout=10).json()
            result = resp.get("result", [])
            if isinstance(result, list):
                return result
        except Exception as e:
            logger.error(f"Tx fetch error: {e}")
        time.sleep(delay * (i+1))
    return []

def fetch_node_stats(address: str) -> dict:
    try:
        r = requests.get(f"{CORTENSOR_API}/stats/node/{address}", timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"Node stats error: {e}")
        return {}


# (… The rest of your job & handler functions stay the same, but reference
# TOKEN, API_KEY, CORTENSOR_API, ADMIN_IDS from the environment variables …)


def main():
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # Register handlers…
    # dp.add_handler(CommandHandler("start", start_command))
    # …

    updater.start_polling()
    logger.info("Bot is running…")
    updater.idle()

if __name__ == "__main__":
    main()