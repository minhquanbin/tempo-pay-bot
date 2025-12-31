# bot.py — Tempo Payment Bot - Railway Optimized with Network Resilience

import time
import sqlite3
import secrets
import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps
from contextlib import contextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

from web3 import Web3
from eth_account import Account
from web3.exceptions import Web3Exception

# ================= CONFIG =================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not found!")
    print("Create .env file with: BOT_TOKEN=your_token_here")
    exit(1)

TEMPO_RPC = "https://rpc.testnet.tempo.xyz"
TEMPO_CHAIN_ID = 42429
TEMPO_FAUCET = "https://docs.tempo.xyz/quickstart/faucet"

DB_FILE = "tempo.db"
DB_TIMEOUT = 30.0

RPC_CALL_DELAY = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 5.0

# Telegram connection settings for Railway
TELEGRAM_CONNECT_TIMEOUT = 60.0
TELEGRAM_READ_TIMEOUT = 60.0
TELEGRAM_WRITE_TIMEOUT = 60.0
TELEGRAM_POOL_TIMEOUT = 60.0

TEMPO_TOKENS = {
    "AlphaUSD": {
        "address": "0x20c0000000000000000000000000000000000001",
        "decimals": 6,
        "symbol": "AUSD",
    },
    "BetaUSD": {
        "address": "0x20c0000000000000000000000000000000000002",
        "decimals": 6,
        "symbol": "BUSD",
    },
    "ThetaUSD": {
        "address": "0x20c0000000000000000000000000000000000003",
        "decimals": 6,
        "symbol": "TUSD",
    },
}

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]

w3 = Web3(Web3.HTTPProvider(TEMPO_RPC))

last_rpc_call = 0
rpc_lock = asyncio.Lock()
db_lock = asyncio.Lock()

def rate_limited_rpc_call(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        global last_rpc_call
        async with rpc_lock:
            now = time.time()
            time_since_last_call = now - last_rpc_call
            if time_since_last_call < RPC_CALL_DELAY:
                wait_time = RPC_CALL_DELAY - time_since_last_call
                await asyncio.sleep(wait_time)
            
            for attempt in range(MAX_RETRIES):
                try:
                    result = func(*args, **kwargs)
                    last_rpc_call = time.time()
                    return result
                except Exception as e:
                    if "429" in str(e) or "Too Many Requests" in str(e):
                        if attempt < MAX_RETRIES - 1:
                            print(f"Rate limit hit, retrying in {RETRY_DELAY}s (attempt {attempt + 1}/{MAX_RETRIES})")
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                    raise
            raise Exception("Max retries exceeded")
    return wrapper

def check_rpc_connection():
    try:
        w3.eth.chain_id
        return True
    except:
        return False

if check_rpc_connection():
    print(f"✓ Connected to Tempo Testnet (Chain ID: {w3.eth.chain_id})")
else:
    print("⚠ Cannot connect to Tempo RPC - will retry on transactions")

bot_instance = None

# ================= DATABASE WITH PROPER LOCKING =================

@contextmanager
def get_db_connection():
    """Context manager for database connections with proper timeout and isolation"""
    conn = None
    try:
        conn = sqlite3.connect(
            DB_FILE,
            timeout=DB_TIMEOUT,
            isolation_level='DEFERRED',
            check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn:
            conn.close()


def init_db():
    """Initialize database with retry logic"""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wallets (
                        telegram_id INTEGER PRIMARY KEY,
                        tempo_address TEXT,
                        tempo_private_key TEXT,
                        ton_address TEXT,
                        notifications_enabled INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recipients (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_id INTEGER,
                        nickname TEXT,
                        address TEXT,
                        blockchain TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(telegram_id, nickname)
                    )
                    """
                )
                
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tx_hash TEXT UNIQUE,
                        from_telegram_id INTEGER,
                        from_address TEXT,
                        to_address TEXT,
                        amount TEXT,
                        token TEXT,
                        memo TEXT,
                        notification_sent INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_state (
                        id INTEGER PRIMARY KEY,
                        last_block INTEGER DEFAULT 0
                    )
                    """
                )
                
                cur.execute("INSERT OR IGNORE INTO sync_state (id, last_block) VALUES (1, 0)")
                
                cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_from ON transactions(from_address)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_to ON transactions(to_address)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_notification ON transactions(notification_sent)")
                
                print("✓ Database initialized successfully")
                return True
                
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                print(f"✗ Database locked, attempt {attempt + 1}/{max_attempts}")
                time.sleep(2 ** attempt)
                continue
            raise e
        except Exception as e:
            print(f"✗ Database initialization error: {e}")
            raise e
    
    raise Exception("Failed to initialize database after maximum attempts")


def get_wallet(tg_id):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT tempo_address, tempo_private_key, ton_address FROM wallets WHERE telegram_id=?",
                (tg_id,),
            )
            row = cur.fetchone()
            return row
    except Exception as e:
        print(f"Error getting wallet: {e}")
        return None


def create_tempo_wallet(tg_id):
    try:
        acct = Account.create(secrets.token_hex(32))
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO wallets (telegram_id, tempo_address, tempo_private_key) VALUES (?,?,?)",
                (tg_id, acct.address, acct.key.hex()),
            )
        return acct.address
    except Exception as e:
        print(f"Error creating wallet: {e}")
        return None


def import_tempo_wallet(tg_id, private_key):
    try:
        acct = Account.from_key(private_key)
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO wallets (telegram_id, tempo_address, tempo_private_key) VALUES (?,?,?)",
                (tg_id, acct.address, private_key),
            )
        return acct.address
    except Exception as e:
        print(f"Import wallet error: {e}")
        return None


def get_telegram_id_by_address(address):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT telegram_id FROM wallets WHERE LOWER(tempo_address)=LOWER(?)",
                (address,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"Error getting telegram ID: {e}")
        return None


def save_transaction(tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO transactions 
                (tx_hash, from_telegram_id, from_address, to_address, amount, token, memo) 
                VALUES (?,?,?,?,?,?,?)""",
                (tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo)
            )
    except Exception as e:
        print(f"Error saving transaction: {e}")


def mark_notification_sent(tx_hash):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE transactions SET notification_sent=1 WHERE tx_hash=?",
                (tx_hash,)
            )
    except Exception as e:
        print(f"Error marking notification: {e}")


def save_recipient(tg_id, nickname, address, blockchain="tempo"):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO recipients (telegram_id, nickname, address, blockchain) VALUES (?,?,?,?)",
                (tg_id, nickname, address, blockchain),
            )
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        print(f"Error saving recipient: {e}")
        return False


def get_recipients(tg_id):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT nickname, address, blockchain FROM recipients WHERE telegram_id=? ORDER BY created_at DESC",
                (tg_id,),
            )
            rows = cur.fetchall()
            return rows
    except Exception as e:
        print(f"Error getting recipients: {e}")
        return []


def get_recipient_by_nickname(tg_id, nickname):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT address, blockchain FROM recipients WHERE telegram_id=? AND nickname=?",
                (tg_id, nickname),
            )
            row = cur.fetchone()
            return row
    except Exception as e:
        print(f"Error getting recipient: {e}")
        return None


def delete_recipient(tg_id, nickname):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM recipients WHERE telegram_id=? AND nickname=?",
                (tg_id, nickname),
            )
            deleted = cur.rowcount > 0
            return deleted
    except Exception as e:
        print(f"Error deleting recipient: {e}")
        return False


# ================= NOTIFICATION SYSTEM =================

async def send_payment_notification(telegram_id, from_addr, amount, token, memo, tx_hash):
    try:
        token_cfg = TEMPO_TOKENS[token]
        explorer_url = f"https://explore.tempo.xyz/tx/{tx_hash}"
        
        from_short = f"{from_addr[:6]}...{from_addr[-4:]}"
        
        message = (
            f"💰 <b>Payment Received!</b>\n\n"
            f"Amount: <b>{amount} {token_cfg['symbol']}</b>\n"
            f"From: <code>{from_short}</code>\n"
            f"Memo: {memo}\n\n"
            f"🔗 <a href='{explorer_url}'>View Transaction</a>"
        )
        
        await bot_instance.send_message(
            chat_id=telegram_id,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        print(f"✓ Notification sent to user {telegram_id}")
        return True
        
    except Exception as e:
        print(f"✗ Failed to send notification: {e}")
        return False


async def check_pending_notifications():
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT tx_hash, from_telegram_id, from_address, to_address, amount, token, memo 
                FROM transactions 
                WHERE notification_sent=0
                LIMIT 10"""
            )
            pending = cur.fetchall()
        
        for tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo in pending:
            recipient_tg_id = get_telegram_id_by_address(to_addr)
            
            if recipient_tg_id and recipient_tg_id != from_tg_id:
                success = await send_payment_notification(
                    recipient_tg_id, from_addr, amount, token, memo, tx_hash
                )
                
                if success:
                    mark_notification_sent(tx_hash)
                    await asyncio.sleep(1)
    except Exception as e:
        print(f"✗ Notification check error: {e}")


async def notification_worker(application):
    global bot_instance
    bot_instance = application.bot
    
    print("✓ Notification worker started")
    
    while True:
        try:
            await check_pending_notifications()
            await asyncio.sleep(30)
        except Exception as e:
            print(f"✗ Notification worker error: {e}")
            await asyncio.sleep(60)


# ================= RATE-LIMITED RPC HELPERS =================

@rate_limited_rpc_call
def get_balance(address):
    return w3.eth.get_balance(address)

@rate_limited_rpc_call
def get_transaction_count(address):
    return w3.eth.get_transaction_count(address)

@rate_limited_rpc_call
def get_gas_price():
    return w3.eth.gas_price

@rate_limited_rpc_call
def send_raw_transaction(raw_tx):
    return w3.eth.send_raw_transaction(raw_tx)


# ================= AUTO DELETE MESSAGE =================

async def delete_message_after_delay(context, chat_id, message_id, delay=60):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        print(f"✓ Auto-deleted message {message_id}")
    except Exception as e:
        print(f"✗ Failed to delete message: {e}")


# ================= UI MENUS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💸 Send Payment", callback_data="send")],
        [InlineKeyboardButton("👛 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📋 Saved Recipients", callback_data="recipients")],
        [InlineKeyboardButton("📊 Transaction History", callback_data="history")],
    ]
    await update.message.reply_text(
        "🚀 <b>Tempo Payment Bot</b>\n\n"
        "Send stablecoins with instant notifications\n\n"
        "Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    w = get_wallet(q.from_user.id)
    if not w or not w[0]:
        keyboard = [
            [InlineKeyboardButton("🆕 Create New Wallet", callback_data="create_wallet")],
            [InlineKeyboardButton("📥 Import Existing Wallet", callback_data="import_wallet")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        await q.message.reply_text(
            "👛 <b>Wallet Setup</b>\n\n"
            "You don't have a wallet yet.\n"
            "Choose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        try:
            native_balance = await get_balance(w[0])
            native_eth = w3.from_wei(native_balance, 'ether')
            
            keyboard = [
                [InlineKeyboardButton("📤 Export Private Key", callback_data="export_key")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]
            
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"💰 <b>Balance:</b>\n"
                f"TEMO: {native_eth:.4f}\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 Get testnet tokens: {TEMPO_FAUCET}"
            )
            
            await q.message.reply_text(
                msg, 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML", 
                disable_web_page_preview=True
            )
        except Exception as e:
            keyboard = [
                [InlineKeyboardButton("📤 Export Private Key", callback_data="export_key")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]
            
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"⚠️ <i>Could not fetch balance (RPC rate limited)</i>\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 Get testnet tokens: {TEMPO_FAUCET}"
            )
            await q.message.reply_text(
                msg, 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML", 
                disable_web_page_preview=True
            )


async def create_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    addr = create_tempo_wallet(q.from_user.id)
    
    if addr:
        await q.message.reply_text(
            f"✅ <b>Tempo wallet created!</b>\n\n"
            f"<code>{addr}</code>\n\n"
            f"💡 Fund this wallet to start sending payments\n"
            f"🚰 Faucet: {TEMPO_FAUCET}\n\n"
            f"🔔 You'll receive notifications when someone sends you payment!",
            parse_mode="HTML"
        )
    else:
        await q.message.reply_text("❌ Failed to create wallet. Please try again.")


async def import_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    context.user_data.clear()
    context.user_data["action"] = "import_wallet"
    
    await q.message.reply_text(
        "📥 <b>Import Wallet</b>\n\n"
        "Send your private key (with or without 0x prefix)\n\n"
        "⚠️ <b>Security:</b>\n"
        "• This message will auto-delete in 60 seconds\n"
        "• Your key will be deleted after import\n"
        "• Never share your private key with anyone!\n\n"
        "Send your private key now:",
        parse_mode="HTML"
    )


async def export_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    w = get_wallet(q.from_user.id)
    if not w or not w[1]:
        await q.message.reply_text("❌ No wallet found")
        return
    
    keyboard = [[InlineKeyboardButton("🗑 Delete Now", callback_data="delete_key_msg")]]
    
    msg = await q.message.reply_text(
        f"🔐 <b>Your Private Key</b>\n\n"
        f"<code>{w[1]}</code>\n\n"
        f"⚠️ <b>IMPORTANT:</b>\n"
        f"• Keep this key safe and secret!\n"
        f"• Never share it with anyone\n"
        f"• This message will auto-delete in 60 seconds\n\n"
        f"💾 Save it somewhere secure now!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    
    context.application.create_task(
        delete_message_after_delay(context, msg.chat_id, msg.message_id, 60)
    )


async def delete_key_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🗑 Message deleted")
    
    try:
        await q.message.delete()
    except Exception as e:
        print(f"Failed to delete message: {e}")


async def history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    wallet = get_wallet(q.from_user.id)
    if not wallet:
        await q.message.reply_text("No wallet found")
        return
    
    user_address = wallet[0].lower()
    
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            
            cur.execute(
                """SELECT tx_hash, to_address, amount, token, memo, created_at 
                FROM transactions 
                WHERE LOWER(from_address)=? 
                ORDER BY created_at DESC LIMIT 10""",
                (user_address,)
            )
            sent_txs = cur.fetchall()
            
            cur.execute(
                """SELECT tx_hash, from_address, amount, token, memo, created_at 
                FROM transactions 
                WHERE LOWER(to_address)=? 
                ORDER BY created_at DESC LIMIT 10""",
                (user_address,)
            )
            received_txs = cur.fetchall()
    except Exception as e:
        await q.message.reply_text(f"❌ Error loading history: {e}")
        return
    
    msg = "📊 <b>Transaction History</b>\n\n"
    
    if sent_txs:
        msg += "📤 <b>Sent:</b>\n"
        for tx_hash, to_addr, amount, token, memo, created_at in sent_txs[:5]:
            to_short = f"{to_addr[:6]}...{to_addr[-4:]}"
            msg += f"• {amount} {token} → <code>{to_short}</code>\n"
        msg += "\n"
    
    if received_txs:
        msg += "📥 <b>Received:</b>\n"
        for tx_hash, from_addr, amount, token, memo, created_at in received_txs[:5]:
            from_short = f"{from_addr[:6]}...{from_addr[-4:]}"
            msg += f"• {amount} {token} ← <code>{from_short}</code>\n"
        msg += "\n"
    
    if not sent_txs and not received_txs:
        msg += "<i>No transactions yet</i>"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    
    await q.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def recipients_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    recipients = get_recipients(q.from_user.id)
    
    if not recipients:
        keyboard = [[InlineKeyboardButton("➕ Add Recipient", callback_data="add_recipient")], [InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
        await q.message.reply_text(
            "📋 <b>Saved Recipients</b>\n\n"
            "<i>No saved recipients yet.</i>\n\n"
            "Add recipients to send payments faster!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
    
    msg = "📋 <b>Saved Recipients:</b>\n\n"
    keyboard = []
    
    for nickname, address, blockchain in recipients:
        short_addr = f"{address[:6]}...{address[-4:]}"
        msg += f"👤 <b>{nickname}</b>\n  <code>{short_addr}</code> ({blockchain})\n\n"
        keyboard.append([InlineKeyboardButton(
            f"🗑 Delete {nickname}", 
            callback_data=f"del_recipient:{nickname}"
        )])
    
    keyboard.append([InlineKeyboardButton("➕ Add New", callback_data="add_recipient")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    
    await q.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def add_recipient_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    context.user_data.clear()
    context.user_data["action"] = "add_recipient"
    context.user_data["step"] = "nickname"
    
    await q.message.reply_text(
        "➕ <b>Add New Recipient</b>\n\n"
        "Enter a nickname for this recipient:\n"
        "Example: <i>Alice, Bob, Merchant1</i>",
        parse_mode="HTML"
    )


async def delete_recipient_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    nickname = q.data.split(":")[1]
    
    deleted = delete_recipient(q.from_user.id, nickname)
    
    if deleted:
        await q.message.reply_text(f"✅ Deleted recipient: <b>{nickname}</b>", parse_mode="HTML")
    else:
        await q.message.reply_text(f"❌ Could not delete recipient: {nickname}")
    
    await recipients_menu(update, context)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not get_wallet(q.from_user.id):
        await q.message.reply_text("You don't have a wallet yet. Choose 'My Wallet' first.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{name} ({cfg['symbol']})", callback_data=f"s_token:{name}")]
        for name, cfg in TEMPO_TOKENS.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])

    await q.message.reply_text(
        "💸 <b>Send Payment</b>\n\n"
        "Select token to send:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def choose_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    context.user_data["token"] = q.data.split(":")[1]
    context.user_data["step"] = "recipient_choice"
    
    recipients = get_recipients(q.from_user.id)
    
    keyboard = [[InlineKeyboardButton("✍️ Enter New Address", callback_data="enter_new_address")]]
    
    if recipients:
        keyboard.insert(0, [InlineKeyboardButton("📋 Use Saved Recipient", callback_data="use_saved_recipient")])
    
    await q.message.reply_text(
        f"Selected: <b>{context.user_data['token']}</b>\n\n"
        f"Choose recipient option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def use_saved_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    recipients = get_recipients(q.from_user.id)
    
    keyboard = [
        [InlineKeyboardButton(nickname, callback_data=f"recipient:{nickname}")]
        for nickname, _, _ in recipients
    ]
    
    await q.message.reply_text(
        "Select recipient:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def select_saved_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    nickname = q.data.split(":")[1]
    recipient_data = get_recipient_by_nickname(q.from_user.id, nickname)
    
    if not recipient_data:
        await q.message.reply_text("Recipient not found")
        return
    
    context.user_data["to"] = recipient_data[0]
    context.user_data["recipient_nickname"] = nickname
    context.user_data["step"] = "amount"
    
    await q.message.reply_text(
        f"Recipient: <b>{nickname}</b>\n"
        f"<code>{recipient_data[0]}</code>\n\n"
        f"Enter amount to send:",
        parse_mode="HTML"
    )


async def enter_new_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    context.user_data["step"] = "to"
    
    await q.message.reply_text("Enter recipient address (0x...):")


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await start(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    step = context.user_data.get("step")
    
    if not step and not action:
        return
    
    if action == "import_wallet":
        private_key = update.message.text.strip()
        
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        try:
            await update.message.delete()
        except:
            pass
        
        addr = import_tempo_wallet(update.effective_user.id, private_key)
        
        if addr:
            await update.message.reply_text(
                f"✅ <b>Wallet imported successfully!</b>\n\n"
                f"<code>{addr}</code>\n\n"
                f"🔔 You'll receive notifications when someone sends you payment!",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "❌ <b>Invalid private key</b>\n\n"
                "Please try again with /start",
                parse_mode="HTML"
            )
        
        context.user_data.clear()
        return
    
    if action == "add_recipient":
        if step == "nickname":
            nickname = update.message.text.strip()
            if len(nickname) < 2 or len(nickname) > 20:
                await update.message.reply_text("Nickname must be 2-20 characters")
                return
            
            context.user_data["recipient_nickname"] = nickname
            context.user_data["step"] = "recipient_address"
            await update.message.reply_text(f"Enter address for '<b>{nickname}</b>' (0x...):", parse_mode="HTML")
            return
        
        elif step == "recipient_address":
            address = update.message.text.strip()
            
            if not Web3.is_address(address):
                await update.message.reply_text("Invalid address. Try again:")
                return
            
            nickname = context.user_data["recipient_nickname"]
            success = save_recipient(update.effective_user.id, nickname, address, "tempo")
            
            if success:
                await update.message.reply_text(
                    f"✅ <b>Saved recipient!</b>\n\n"
                    f"<b>{nickname}</b>\n"
                    f"<code>{address}</code>\n\n"
                    f"🔔 They'll get notified when you send them payment!",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"❌ Nickname '<b>{nickname}</b>' already exists", parse_mode="HTML")
            
            context.user_data.clear()
            return
    
    if step == "to":
        to_address = update.message.text.strip()
        
        if not Web3.is_address(to_address):
            await update.message.reply_text("Invalid address. Please try again:")
            return
        
        context.user_data["to"] = to_address
        context.user_data["step"] = "amount"
        await update.message.reply_text("Enter amount to send:")
        return

    elif step == "amount":
        try:
            amount = float(update.message.text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
            context.user_data["amount"] = amount
        except:
            await update.message.reply_text("Invalid amount. Please try again:")
            return

        context.user_data["step"] = "memo"
        await update.message.reply_text(
            "Enter payment memo:\n\n"
            "Example: <i>INVOICE123456</i>\n"
            "Or: <i>Payment for services</i>\n\n"
            "This memo will be stored onchain",
            parse_mode="HTML"
        )
        return

    elif step == "memo":
        memo = update.message.text.strip()
        
        if not memo:
            await update.message.reply_text("Memo cannot be empty. Please try again:")
            return

        processing_msg = await update.message.reply_text("⏳ Processing transaction...\n<i>This may take 10-15 seconds due to rate limits</i>", parse_mode="HTML")

        token = context.user_data.get("token")
        to = context.user_data.get("to")
        amount = context.user_data.get("amount")
        recipient_nickname = context.user_data.get("recipient_nickname", "")
        
        if not token or not to or not amount:
            await processing_msg.edit_text("❌ Session expired. Please start over with /start")
            context.user_data.clear()
            return

        wallet = get_wallet(update.effective_user.id)
        if not wallet or not wallet[0]:
            await processing_msg.edit_text("❌ Wallet not found")
            context.user_data.clear()
            return

        acct = Account.from_key(wallet[1])
        cfg = TEMPO_TOKENS[token]

        try:
            native_balance = await get_balance(acct.address)
            if native_balance == 0:
                raise Exception(f"Insufficient TEMO for gas. Get testnet tokens: {TEMPO_FAUCET}")

            nonce = await get_transaction_count(acct.address)
            gas_price = await get_gas_price()
            
            raw_amount = int(amount * (10 ** cfg["decimals"]))

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(cfg["address"]),
                abi=ERC20_ABI
            )

            transfer_function = contract.functions.transfer(
                Web3.to_checksum_address(to), 
                raw_amount
            )

            tx_dict = {
                "from": acct.address,
                "to": Web3.to_checksum_address(cfg["address"]),
                "value": 0,
                "gas": 200000,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": TEMPO_CHAIN_ID,
                "data": transfer_function._encode_transaction_data(),
            }

            memo_bytes = memo.encode('utf-8')
            memo_hex = memo_bytes.hex()
            tx_dict["data"] = tx_dict["data"] + memo_hex

            signed_tx = w3.eth.account.sign_transaction(tx_dict, acct.key)
            tx_hash = await send_raw_transaction(signed_tx.rawTransaction)
            
            tx_hash_hex = tx_hash.hex()
            explorer_url = f"https://explore.tempo.xyz/tx/{tx_hash_hex}"

            save_transaction(
                tx_hash_hex,
                update.effective_user.id,
                acct.address,
                to,
                str(amount),
                token,
                memo
            )

            if recipient_nickname:
                recipient_display = f"{recipient_nickname}\n{to[:10]}...{to[-8:]}"
            else:
                recipient_display = f"{to[:10]}...{to[-8:]}"

            await processing_msg.edit_text(
                f"✅ <b>Payment sent successfully!</b>\n\n"
                f"💰 Token: <b>{token}</b>\n"
                f"📊 Amount: <b>{amount} {cfg['symbol']}</b>\n"
                f"👤 Recipient: <code>{recipient_display}</code>\n"
                f"📝 Memo: <i>{memo}</i>\n\n"
                f"🔗 <a href='{explorer_url}'>View on Explorer</a>\n\n"
                f"🔔 Recipient will be notified if they use this bot!",
                parse_mode="HTML",
                disable_web_page_preview=True
            )

        except Exception as e:
            error_msg = str(e)
            print(f"✗ Transaction error: {error_msg}")
            
            if "429" in error_msg or "Too Many Requests" in error_msg:
                error_display = "⚠️ RPC rate limit reached. Please try again in 30 seconds."
            elif "insufficient funds" in error_msg.lower():
                error_display = "❌ Insufficient TEMO for gas fees"
            elif "nonce" in error_msg.lower():
                error_display = "❌ Transaction nonce error. Please try again."
            else:
                error_display = f"❌ {error_msg[:200]}"
            
            await processing_msg.edit_text(
                f"<b>Transaction failed</b>\n\n"
                f"{error_display}\n\n"
                f"📋 <b>Checklist:</b>\n"
                f"• Wallet has {token}?\n"
                f"• Wallet has TEMO for gas?\n"
                f"• Try again in 30 seconds\n\n"
                f"🚰 Get testnet tokens: {TEMPO_FAUCET}",
                parse_mode="HTML",
                disable_web_page_preview=True
            )

        context.user_data.clear()
        return


async def post_init(application):
    asyncio.create_task(notification_worker(application))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates."""
    print(f"✗ Update {update} caused error {context.error}")


def main():
    # Clean up old database if schema is outdated
    if os.path.exists(DB_FILE):
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(wallets)")
                columns = [row[1] for row in cur.fetchall()]
                if 'tempo_address' not in columns:
                    raise sqlite3.OperationalError("Old schema")
                cur.execute("SELECT tempo_address FROM wallets LIMIT 1")
        except (sqlite3.OperationalError, Exception) as e:
            print(f"⚠️  Old or locked database detected: {e}")
            print("⚠️  Removing old database...")
            try:
                os.remove(DB_FILE)
                print("✓ Old database removed. Creating new one...")
            except Exception as remove_error:
                print(f"✗ Could not remove database: {remove_error}")
                print("⚠️  Attempting to continue anyway...")
    
    # Initialize database with retry logic
    try:
        init_db()
    except Exception as e:
        print(f"✗ Failed to initialize database: {e}")
        print("⚠️  Bot may not function correctly")

    # Configure request with extended timeouts for Railway
    request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )

    # Build application with custom request configuration
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .post_init(post_init)
        .build()
    )

    # Add error handler
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(wallet_menu, pattern="^wallet$"))
    app.add_handler(CallbackQueryHandler(create_wallet_handler, pattern="^create_wallet$"))
    app.add_handler(CallbackQueryHandler(import_wallet_handler, pattern="^import_wallet$"))
    app.add_handler(CallbackQueryHandler(export_key_handler, pattern="^export_key$"))
    app.add_handler(CallbackQueryHandler(delete_key_message, pattern="^delete_key_msg$"))
    app.add_handler(CallbackQueryHandler(send_menu, pattern="^send$"))
    app.add_handler(CallbackQueryHandler(recipients_menu, pattern="^recipients$"))
    app.add_handler(CallbackQueryHandler(history_menu, pattern="^history$"))
    app.add_handler(CallbackQueryHandler(add_recipient_start, pattern="^add_recipient$"))
    app.add_handler(CallbackQueryHandler(delete_recipient_confirm, pattern="^del_recipient:"))
    app.add_handler(CallbackQueryHandler(choose_token, pattern="^s_token:"))
    app.add_handler(CallbackQueryHandler(use_saved_recipient, pattern="^use_saved_recipient$"))
    app.add_handler(CallbackQueryHandler(select_saved_recipient, pattern="^recipient:"))
    app.add_handler(CallbackQueryHandler(enter_new_address, pattern="^enter_new_address$"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_main$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("=" * 50)
    print("🚀 Tempo Payment Bot - Railway Optimized")
    print("=" * 50)
    print(f"✓ Tempo RPC: {TEMPO_RPC}")
    print(f"✓ Chain ID: {TEMPO_CHAIN_ID}")
    print(f"✓ Database: WAL mode with {DB_TIMEOUT}s timeout")
    print(f"✓ Telegram timeouts: {TELEGRAM_CONNECT_TIMEOUT}s")
    print(f"✓ Import/Export Wallet: Enabled")
    print(f"✓ Auto-delete sensitive messages: 60s")
    print(f"✓ Notification system: Active")
    print("=" * 50)
    print("Bot is running... Press Ctrl+C to stop")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()