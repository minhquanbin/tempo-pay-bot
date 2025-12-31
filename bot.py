# bot.py — Tempo Payment Bot - Timeout Fixed (ALL FEATURES KEPT)

import time
import sqlite3
import secrets
import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps
from contextlib import contextmanager
import concurrent.futures

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# ================= CONFIG =================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not found!")
    exit(1)

TEMPO_RPC = "https://rpc.testnet.tempo.xyz"
TEMPO_CHAIN_ID = 41454
TEMPO_FAUCET = "https://docs.tempo.xyz/quickstart/faucet"
DB_FILE = "tempo.db"
DB_TIMEOUT = 30.0

RPC_CALL_DELAY = 0.5  # Giảm từ 2.0 xuống 0.5
MAX_RETRIES = 2  # Giảm từ 3 xuống 2
RETRY_DELAY = 2.0  # Giảm từ 5.0 xuống 2.0

TELEGRAM_CONNECT_TIMEOUT = 30.0  # Giảm từ 60
TELEGRAM_READ_TIMEOUT = 30.0
TELEGRAM_WRITE_TIMEOUT = 30.0
TELEGRAM_POOL_TIMEOUT = 30.0

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
bot_instance = None

# Thread pool for blocking RPC calls
executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)

# ================= RPC HELPERS (NON-BLOCKING) =================

async def async_rpc_call(func, *args, **kwargs):
    """Run blocking RPC call in thread pool"""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, func, *args, **kwargs),
            timeout=5.0  # 5s timeout cho mỗi RPC call
        )
        return result
    except asyncio.TimeoutError:
        print(f"RPC timeout: {func.__name__}")
        return None
    except Exception as e:
        print(f"RPC error {func.__name__}: {e}")
        return None


async def get_balance_async(address):
    """Non-blocking get balance"""
    return await async_rpc_call(w3.eth.get_balance, address)


async def get_transaction_count_async(address):
    """Non-blocking get transaction count"""
    return await async_rpc_call(w3.eth.get_transaction_count, address)


async def get_gas_price_async():
    """Non-blocking get gas price"""
    return await async_rpc_call(lambda: w3.eth.gas_price)


async def send_raw_transaction_async(raw_tx):
    """Non-blocking send transaction"""
    return await async_rpc_call(w3.eth.send_raw_transaction, raw_tx)


# ================= DATABASE =================

@contextmanager
def get_db_connection():
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
    with get_db_connection() as conn:
        cur = conn.cursor()
        
        cur.execute(
            """CREATE TABLE IF NOT EXISTS wallets (
                telegram_id INTEGER PRIMARY KEY,
                tempo_address TEXT,
                tempo_private_key TEXT,
                ton_address TEXT,
                notifications_enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        
        cur.execute(
            """CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                nickname TEXT,
                address TEXT,
                blockchain TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, nickname)
            )"""
        )
        
        cur.execute(
            """CREATE TABLE IF NOT EXISTS transactions (
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
            )"""
        )
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_from ON transactions(from_address)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_to ON transactions(to_address)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_notification ON transactions(notification_sent)")
        
    print("Database initialized")


def get_wallet(tg_id):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tempo_address, tempo_private_key FROM wallets WHERE telegram_id=?", (tg_id,))
        return cur.fetchone()


def create_tempo_wallet(tg_id):
    acct = Account.create(secrets.token_hex(32))
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO wallets (telegram_id, tempo_address, tempo_private_key) VALUES (?,?,?)",
            (tg_id, acct.address, acct.key.hex()),
        )
    return acct.address


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
        print(f"Import error: {e}")
        return None


def get_telegram_id_by_address(address):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM wallets WHERE LOWER(tempo_address)=LOWER(?)", (address,))
        row = cur.fetchone()
        return row[0] if row else None


def save_transaction(tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO transactions 
            (tx_hash, from_telegram_id, from_address, to_address, amount, token, memo) 
            VALUES (?,?,?,?,?,?,?)""",
            (tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo)
        )


def mark_notification_sent(tx_hash):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE transactions SET notification_sent=1 WHERE tx_hash=?", (tx_hash,))


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


def get_recipients(tg_id):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT nickname, address, blockchain FROM recipients WHERE telegram_id=? ORDER BY created_at DESC", (tg_id,))
        return cur.fetchall()


def get_recipient_by_nickname(tg_id, nickname):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT address, blockchain FROM recipients WHERE telegram_id=? AND nickname=?", (tg_id, nickname))
        return cur.fetchone()


def delete_recipient(tg_id, nickname):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM recipients WHERE telegram_id=? AND nickname=?", (tg_id, nickname))
        return cur.rowcount > 0


# ================= NOTIFICATION =================

async def send_instant_notification(bot, telegram_id, from_addr, amount, token, memo, tx_hash):
    try:
        token_cfg = TEMPO_TOKENS.get(token)
        if not token_cfg:
            return False
            
        explorer_url = f"https://explore.tempo.xyz/tx/{tx_hash}"
        from_short = f"{from_addr[:6]}...{from_addr[-4:]}"
        
        message = (
            f"💰 <b>Payment Received!</b>\n\n"
            f"📥 Amount: <b>{amount} {token_cfg['symbol']}</b>\n"
            f"📤 From: <code>{from_short}</code>\n"
            f"📝 Memo: <i>{memo}</i>\n\n"
            f"🔗 <a href='{explorer_url}'>View Transaction</a>"
        )
        
        await bot.send_message(chat_id=telegram_id, text=message, parse_mode="HTML", disable_web_page_preview=True)
        print(f"Notification sent to {telegram_id}")
        return True
    except Exception as e:
        print(f"Notification failed: {e}")
        return False


async def check_pending_notifications():
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT tx_hash, from_telegram_id, from_address, to_address, amount, token, memo 
                FROM transactions WHERE notification_sent=0 LIMIT 10"""
            )
            pending = cur.fetchall()
        
        for tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo in pending:
            recipient_tg_id = get_telegram_id_by_address(to_addr)
            
            if recipient_tg_id and recipient_tg_id != from_tg_id:
                success = await send_instant_notification(
                    bot_instance, recipient_tg_id, from_addr, amount, token, memo, tx_hash
                )
                if success:
                    mark_notification_sent(tx_hash)
                    await asyncio.sleep(1)
    except Exception as e:
        print(f"Notification check error: {e}")


async def notification_worker(application):
    global bot_instance
    bot_instance = application.bot
    print("Notification worker started")
    
    while True:
        try:
            await check_pending_notifications()
            await asyncio.sleep(30)
        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(60)


# ================= AUTO DELETE =================

async def delete_message_after_delay(context, chat_id, message_id, delay=60):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        print(f"Auto-deleted message {message_id}")
    except Exception as e:
        print(f"Delete failed: {e}")


# ================= UI =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💸 Send Payment", callback_data="send")],
        [InlineKeyboardButton("👛 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("📋 Saved Recipients", callback_data="recipients")],
        [InlineKeyboardButton("📊 Transaction History", callback_data="history")],
    ]
    await update.message.reply_text(
        "🚀 <b>Tempo Payment Bot</b>\n\nSend stablecoins with instant notifications\n\nChoose an option:",
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
            [InlineKeyboardButton("📥 Import Wallet", callback_data="import_wallet")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        await q.message.reply_text(
            "👛 <b>Wallet Setup</b>\n\nYou don't have a wallet yet.\nChoose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        # Check balance in background (non-blocking)
        balance = await get_balance_async(w[0])
        
        keyboard = [
            [InlineKeyboardButton("📤 Export Private Key", callback_data="export_key")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ]
        
        if balance is not None:
            native_eth = w3.from_wei(balance, 'ether')
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"💰 <b>Balance:</b>\n"
                f"TEMO: {native_eth:.4f}\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 <a href='{TEMPO_FAUCET}'>Get testnet tokens</a>"
            )
        else:
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"⚠️ <i>Could not fetch balance (RPC busy)</i>\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 <a href='{TEMPO_FAUCET}'>Get testnet tokens</a>"
            )
        
        await q.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML", disable_web_page_preview=True)


async def create_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    addr = create_tempo_wallet(q.from_user.id)
    await q.message.reply_text(
        f"✅ <b>Wallet created!</b>\n\n"
        f"<code>{addr}</code>\n\n"
        f"💡 Fund this wallet to start\n"
        f"🚰 Faucet: {TEMPO_FAUCET}\n\n"
        f"🔔 You'll receive notifications!",
        parse_mode="HTML"
    )


async def import_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    context.user_data.clear()
    context.user_data["action"] = "import_wallet"
    
    await q.message.reply_text(
        "📥 <b>Import Wallet</b>\n\n"
        "Send your private key (with or without 0x)\n\n"
        "⚠️ <b>Security:</b>\n"
        "• Auto-delete in 60s\n"
        "• Never share your key!\n\n"
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
        f"⚠️ Keep safe!\n"
        f"• Auto-delete in 60s\n\n"
        f"💾 Save it now!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    
    context.application.create_task(delete_message_after_delay(context, msg.chat_id, msg.message_id, 60))


async def delete_key_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🗑 Deleted")
    try:
        await q.message.delete()
    except:
        pass


async def history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    wallet = get_wallet(q.from_user.id)
    if not wallet:
        await q.message.reply_text("❌ No wallet found")
        return
    
    user_address = wallet[0].lower()
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT tx_hash, to_address, amount, token FROM transactions 
            WHERE LOWER(from_address)=? ORDER BY created_at DESC LIMIT 5""",
            (user_address,)
        )
        sent_txs = cur.fetchall()
        
        cur.execute(
            """SELECT tx_hash, from_address, amount, token FROM transactions 
            WHERE LOWER(to_address)=? ORDER BY created_at DESC LIMIT 5""",
            (user_address,)
        )
        received_txs = cur.fetchall()
    
    msg = "📊 <b>Transaction History</b>\n\n"
    
    if sent_txs:
        msg += "📤 <b>Sent:</b>\n"
        for tx_hash, to_addr, amount, token in sent_txs:
            to_short = f"{to_addr[:6]}...{to_addr[-4:]}"
            msg += f"• {amount} {token} → <code>{to_short}</code>\n"
        msg += "\n"
    
    if received_txs:
        msg += "📥 <b>Received:</b>\n"
        for tx_hash, from_addr, amount, token in received_txs:
            from_short = f"{from_addr[:6]}...{from_addr[-4:]}"
            msg += f"• {amount} {token} ← <code>{from_short}</code>\n"
        msg += "\n"
    
    if not sent_txs and not received_txs:
        msg += "<i>No transactions yet</i>"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await q.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def recipients_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    recipients = get_recipients(q.from_user.id)
    
    if not recipients:
        keyboard = [[InlineKeyboardButton("➕ Add Recipient", callback_data="add_recipient")], [InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
        await q.message.reply_text(
            "📋 <b>Saved Recipients</b>\n\n<i>No saved recipients yet.</i>\n\nAdd recipients to send faster!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return
    
    msg = "📋 <b>Saved Recipients:</b>\n\n"
    keyboard = []
    
    for nickname, address, blockchain in recipients:
        short_addr = f"{address[:6]}...{address[-4:]}"
        msg += f"👤 <b>{nickname}</b>\n  <code>{short_addr}</code> ({blockchain})\n\n"
        keyboard.append([InlineKeyboardButton(f"🗑 Delete {nickname}", callback_data=f"del_recipient:{nickname}")])
    
    keyboard.append([InlineKeyboardButton("➕ Add New", callback_data="add_recipient")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    
    await q.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def add_recipient_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    context.user_data.clear()
    context.user_data["action"] = "add_recipient"
    context.user_data["step"] = "nickname"
    
    await q.message.reply_text("➕ <b>Add Recipient</b>\n\nEnter nickname:\nExample: <i>Alice, Bob</i>", parse_mode="HTML")


async def delete_recipient_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    nickname = q.data.split(":")[1]
    deleted = delete_recipient(q.from_user.id, nickname)
    
    if deleted:
        await q.message.reply_text(f"✅ Deleted: <b>{nickname}</b>", parse_mode="HTML")
    else:
        await q.message.reply_text(f"❌ Could not delete: {nickname}")
    
    await recipients_menu(update, context)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not get_wallet(q.from_user.id):
        await q.message.reply_text("❌ No wallet. Choose 'My Wallet' first.")
        return

    keyboard = [
        [InlineKeyboardButton(f"{name} ({cfg['symbol']})", callback_data=f"s_token:{name}")]
        for name, cfg in TEMPO_TOKENS.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])

    await q.message.reply_text("💸 <b>Send Payment</b>\n\nSelect token:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def choose_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    context.user_data["token"] = q.data.split(":")[1]
    context.user_data["step"] = "recipient_choice"
    
    recipients = get_recipients(q.from_user.id)
    keyboard = [[InlineKeyboardButton("✍️ Enter Address", callback_data="enter_new_address")]]
    
    if recipients:
        keyboard.insert(0, [InlineKeyboardButton("📋 Use Saved", callback_data="use_saved_recipient")])
    
    await q.message.reply_text(
        f"Selected: <b>{context.user_data['token']}</b>\n\nChoose option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def use_saved_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    recipients = get_recipients(q.from_user.id)
    keyboard = [[InlineKeyboardButton(nickname, callback_data=f"recipient:{nickname}")] for nickname, _, _ in recipients]
    await q.message.reply_text("📋 Select:", reply_markup=InlineKeyboardMarkup(keyboard))


async def select_saved_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    nickname = q.data.split(":")[1]
    recipient_data = get_recipient_by_nickname(q.from_user.id, nickname)
    
    if not recipient_data:
        await q.message.reply_text("❌ Not found")
        return
    
    context.user_data["to"] = recipient_data[0]
    context.user_data["recipient_nickname"] = nickname
    context.user_data["step"] = "amount"
    
    await q.message.reply_text(f"✅ Recipient: <b>{nickname}</b>\n<code>{recipient_data[0]}</code>\n\nEnter amount:", parse_mode="HTML")


async def enter_new_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["step"] = "to"
    await q.message.reply_text("Enter address (0x...):")


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await start(update, context)


# ================= DEBUG =================

async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = get_wallet(update.effective_user.id)
    if wallet:
        await update.message.reply_text(
            f"✅ <b>Registered!</b>\n\nAddress: <code>{wallet[0]}</code>\nTelegram ID: <code>{update.effective_user.id}</code>\n\n🔔 Notifications enabled!",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ <b>Not registered</b>\n\nUse /start to create wallet.", parse_mode="HTML")


async def test_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Testing...")
    try:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="✅ <b>Test successful!</b> 🎉\n\nNotification system working!",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Working!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


# ================= TEXT HANDLER =================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    step = context.user_data.get("step")
    
    if not step and not action:
        return
    
    # IMPORT WALLET
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
                f"✅ <b>Imported!</b>\n\n<code>{addr}</code>\n\n🔔 Notifications enabled!",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ <b>Invalid key</b>\n\nTry /start", parse_mode="HTML")
        
        context.user_data.clear()
        return
    
    # ADD RECIPIENT
    if action == "add_recipient":
        if step == "nickname":
            nickname = update.message.text.strip()
            if len(nickname) < 2 or len(nickname) > 20:
                await update.message.reply_text("❌ Nickname 2-20 chars")
                return
            
            context.user_data["recipient_nickname"] = nickname
            context.user_data["step"] = "recipient_address"
            await update.message.reply_text(f"Enter address for '<b>{nickname}</b>' (0x...):", parse_mode="HTML")
            return
        
        elif step == "recipient_address":
            address = update.message.text.strip()
            
            if not Web3.is_address(address):
                await update.message.reply_text("❌ Invalid. Try again:")
                return
            
            nickname = context.user_data["recipient_nickname"]
            success = save_recipient(update.effective_user.id, nickname, address, "tempo")
            
            if success:
                await update.message.reply_text(
                    f"✅ <b>Saved!</b>\n\n<b>{nickname}</b>\n<code>{address}</code>\n\n🔔 Notified when you send!",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(f"❌ '<b>{nickname}</b>' exists", parse_mode="HTML")
            
            context.user_data.clear()
            return
    
    # SEND PAYMENT
    if step == "to":
        to_address = update.message.text.strip()
        
        if not Web3.is_address(to_address):
            await update.message.reply_text("❌ Invalid. Try again:")
            return
        
        context.user_data["to"] = to_address
        context.user_data["step"] = "amount"
        await update.message.reply_text("Enter amount:")
        return

    elif step == "amount":
        try:
            amount = float(update.message.text)
            if amount <= 0:
                raise ValueError()
            context.user_data["amount"] = amount
        except:
            await update.message.reply_text("❌ Invalid. Try again:")
            return

        context.user_data["step"] = "memo"
        await update.message.reply_text("📝 <b>Enter memo:</b>\n\nExample: <i>INVOICE123</i>\n\n💡 Stored onchain", parse_mode="HTML")
        return

    elif step == "memo":
        memo = update.message.text.strip()
        
        if not memo:
            await update.message.reply_text("❌ Cannot be empty:")
            return

        processing_msg = await update.message.reply_text("⏳ <b>Processing...</b>", parse_mode="HTML")

        token = context.user_data.get("token")
        to = context.user_data.get("to")
        amount = context.user_data.get("amount")
        recipient_nickname = context.user_data.get("recipient_nickname", "")
        
        if not token or not to or not amount:
            await processing_msg.edit_text("❌ Session expired. /start")
            context.user_data.clear()
            return

        wallet = get_wallet(update.effective_user.id)
        if not wallet:
            await processing_msg.edit_text("❌ Wallet not found")
            context.user_data.clear()
            return

        acct = Account.from_key(wallet[1])
        cfg = TEMPO_TOKENS[token]

        try:
            # Use async RPC calls (non-blocking)
            native_balance = await get_balance_async(acct.address)
            if native_balance is None or native_balance == 0:
                raise Exception(f"Insufficient TEMO. Faucet: {TEMPO_FAUCET}")

            nonce = await get_transaction_count_async(acct.address)
            if nonce is None:
                raise Exception("RPC error: Could not get nonce")
            
            gas_price = await get_gas_price_async()
            if gas_price is None:
                raise Exception("RPC error: Could not get gas price")
            
            raw_amount = int(amount * (10 ** cfg["decimals"]))

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(cfg["address"]),
                abi=ERC20_ABI
            )

            transfer_function = contract.functions.transfer(Web3.to_checksum_address(to), raw_amount)

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
            
            tx_hash = await send_raw_transaction_async(signed_tx.rawTransaction)
            if tx_hash is None:
                raise Exception("RPC error: Could not send transaction")
            
            tx_hash_hex = tx_hash.hex()
            explorer_url = f"https://explore.tempo.xyz/tx/{tx_hash_hex}"

            save_transaction(tx_hash_hex, update.effective_user.id, acct.address, to, str(amount), token, memo)

            # Send notification
            recipient_tg_id = get_telegram_id_by_address(to)
            
            if recipient_tg_id and recipient_tg_id != update.effective_user.id:
                await send_instant_notification(context.bot, recipient_tg_id, acct.address, str(amount), token, memo, tx_hash_hex)
                mark_notification_sent(tx_hash_hex)

            if recipient_nickname:
                recipient_display = f"<b>{recipient_nickname}</b>\n<code>{to[:10]}...{to[-8:]}</code>"
            else:
                recipient_display = f"<code>{to[:10]}...{to[-8:]}</code>"

            await processing_msg.edit_text(
                f"✅ <b>Payment sent!</b>\n\n"
                f"💰 Token: <b>{token}</b>\n"
                f"📊 Amount: <b>{amount} {cfg['symbol']}</b>\n"
                f"👤 Recipient: {recipient_display}\n"
                f"📝 Memo: <i>{memo}</i>\n\n"
                f"🔗 <a href='{explorer_url}'>View on Explorer</a>\n\n"
                f"{'🔔 Recipient notified!' if recipient_tg_id and recipient_tg_id != update.effective_user.id else '💡 Recipient will be notified if they use bot'}",
                parse_mode="HTML",
                disable_web_page_preview=True
            )

        except Exception as e:
            error_msg = str(e)[:200]
            await processing_msg.edit_text(
                f"❌ <b>Failed</b>\n\n{error_msg}\n\n📋 <b>Checklist:</b>\n• Has {token}?\n• Has TEMO for gas?\n\n🚰 {TEMPO_FAUCET}",
                parse_mode="HTML",
                disable_web_page_preview=True
            )

        context.user_data.clear()
        return


# ================= MAIN =================

async def post_init(application):
    asyncio.create_task(notification_worker(application))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Error: {context.error}")


def main():
    if os.path.exists(DB_FILE):
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(wallets)")
                columns = [row[1] for row in cur.fetchall()]
                if 'tempo_address' not in columns:
                    raise Exception("Old schema")
        except:
            try:
                os.remove(DB_FILE)
                print("Old database removed")
            except:
                pass
    
    init_db()

    request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=TELEGRAM_POOL_TIMEOUT,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_user))
    app.add_handler(CommandHandler("test", test_notification))
    
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

    print("=" * 60)
    print("🚀 Tempo Payment Bot - ALL FEATURES")
    print("=" * 60)
    print(f"✓ RPC: {TEMPO_RPC}")
    print(f"✓ Chain: {TEMPO_CHAIN_ID}")
    print(f"✓ Async RPC: ThreadPoolExecutor")
    print(f"✓ Database: WAL mode")
    print(f"✓ Instant Notifications: ENABLED")
    print(f"✓ Auto-delete messages: ENABLED")
    print(f"✓ Import/Export wallet: ENABLED")
    print(f"✓ Debug commands: /check /test")
    print("=" * 60)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()