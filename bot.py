import os
import time
import logging
import requests
from telegram import Update, ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackContext
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Constants
API_KEY = os.getenv("TELEGRAM_API_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
MAX_WALLETS = 5

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory data store
user_data = {}

# Helper functions
def shorten_address(address):
    return f"{address[:6]}...{address[-4:]}"

def get_eth_balance(address):
    url = f"https://api.arbiscan.io/api?module=account&action=balance&address={address}&tag=latest&apikey={ETHERSCAN_API_KEY}"
    try:
        response = requests.get(url).json()
        if response['status'] == '1':
            return round(int(response['result']) / 1e18, 4)
        else:
            return 0.0
    except Exception as e:
        logger.warning(f"Error getting balance: {e}")
        return 0.0

def fetch_transactions(address):
    url = f"https://api.arbiscan.io/api?module=account&action=txlist&address={address}&startblock=0&endblock=99999999&sort=desc&apikey={ETHERSCAN_API_KEY}"
    try:
        response = requests.get(url).json()
        if response['status'] == '1':
            return response['result']
        else:
            return []
    except Exception as e:
        logger.warning(f"Error fetching transactions: {e}")
        return []

def check_stall_status(txs):
    ping_method = "0x5c36b186"
    last_25 = txs[:25]
    if all(tx['input'].startswith(ping_method) for tx in last_25):
        return True
    return False

def get_last_successful_method_time(txs):
    method_map = {
        "0xf21a494b": "Commit",
        "0x65c815a5": "Precommit",
        "0xca6726d9": "Prepare",
        "0x198e2b8a": "Create"
    }
    for tx in txs:
        if tx['isError'] == '0' and tx['input'][:10] in method_map:
            method_name = method_map[tx['input'][:10]]
            timestamp = int(tx['timeStamp'])
            ago = int((time.time() - timestamp) / 60)
            return f"(last successful {method_name} transaction was {ago} mins ago)"
    return "(no recent successful Commit/Precommit/Prepare/Create transaction)"

# Bot Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /add <wallet>,<label> to start.")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_data:
        user_data[chat_id] = []

    if len(user_data[chat_id]) >= MAX_WALLETS:
        await update.message.reply_text("You've reached the maximum number of wallets (5).")
        return

    try:
        arg = ' '.join(context.args)
        wallet, label = [x.strip() for x in arg.split(",")]
        user_data[chat_id].append((wallet, label))
        await update.message.reply_text(f"Wallet {label} added.")
    except:
        await update.message.reply_text("Invalid format. Use: /add <wallet>,<label>")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_data or len(user_data[chat_id]) == 0:
        await update.message.reply_text("No wallets added.")
        return

    message = "Auto Update\n\n"
    for idx, (address, label) in enumerate(user_data[chat_id], 1):
        txs = fetch_transactions(address)
        balance = get_eth_balance(address)
        stall = check_stall_status(txs)
        last_method = get_last_successful_method_time(txs)
        short = shorten_address(address)

        message += (
            f"🔑 {short} (Node {idx})\n"
            f"💰 Balance: {balance} ETH | Status: 🟢 Online\n"
            f"⏱️ Last Activity: {int((time.time() - int(txs[0]['timeStamp'])) / 60)} mins ago\n"
            f"🩺 Health: 🟩 🟩 🟩 🟩 🟩\n"
            f"⚠️ Stall: {'✅ Normal' if not stall else '❌ Stalled'}\n"
            f"Transaction: {last_method}\n"
            f"🔗 Arbiscan | 📈 Dashboard\n\n"
        )

    if len(message) > 4096:
        for i in range(0, len(message), 4096):
            await update.message.reply_text(message[i:i+4096], parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

# Main
if __name__ == '__main__':
    app = ApplicationBuilder().token(API_KEY).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("status", status))
    app.run_polling()
