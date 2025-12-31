# bot.py — Tempo Payment Bot with Rate Limiting Fix

import time
import sqlite3
import secrets
import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps

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

from web3 import Web3
from eth_account import Account
from web3.exceptions import Web3Exception

# ================= CONFIG =================

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not found!")
    print("Create .env file with: BOT_TOKEN=your_token_here")
    exit(1)

# Tempo Configuration
TEMPO_RPC = "https://rpc.testnet.tempo.xyz"
TEMPO_CHAIN_ID = 42429
TEMPO_FAUCET = "https://docs.tempo.xyz/quickstart/faucet"

# Database
DB_FILE = "tempo.db"

# Rate Limiting Configuration
RPC_CALL_DELAY = 2.0  # Seconds between RPC calls
MAX_RETRIES = 3
RETRY_DELAY = 5.0  # Seconds to wait before retry

# Tempo Tokens
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

# ERC20 ABI
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

# Initialize Web3
w3 = Web3(Web3.HTTPProvider(TEMPO_RPC))

# Global rate limiting
last_rpc_call = 0
rpc_lock = asyncio.Lock()

def rate_limited_rpc_call(func):
    """Decorator to rate limit RPC calls"""
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

# Global variable to store bot instance for notifications
bot_instance = None

# ================= DATABASE =================

def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    
    conn.commit()
    conn.close()


def get_wallet(tg_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT tempo_address, tempo_private_key, ton_address FROM wallets WHERE telegram_id=?",
        (tg_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def create_tempo_wallet(tg_id):
    acct = Account.create(secrets.token_hex(32))
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO wallets (telegram_id, tempo_address, tempo_private_key) VALUES (?,?,?)",
        (tg_id, acct.address, acct.key.hex()),
    )
    conn.commit()
    conn.close()
    return acct.address


def get_telegram_id_by_address(address):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT telegram_id FROM wallets WHERE LOWER(tempo_address)=LOWER(?)",
        (address,),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def save_transaction(tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO transactions 
            (tx_hash, from_telegram_id, from_address, to_address, amount, token, memo) 
            VALUES (?,?,?,?,?,?,?)""",
            (tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


def mark_notification_sent(tx_hash):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE transactions SET notification_sent=1 WHERE tx_hash=?",
        (tx_hash,)
    )
    conn.commit()
    conn.close()


def save_recipient(tg_id, nickname, address, blockchain="tempo"):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO recipients (telegram_id, nickname, address, blockchain) VALUES (?,?,?,?)",
            (tg_id, nickname, address, blockchain),
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success


def get_recipients(tg_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT nickname, address, blockchain FROM recipients WHERE telegram_id=? ORDER BY created_at DESC",
        (tg_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_recipient_by_nickname(tg_id, nickname):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT address, blockchain FROM recipients WHERE telegram_id=? AND nickname=?",
        (tg_id, nickname),
    )
    row = cur.fetchone()
    conn.close()
    return row


def delete_recipient(tg_id, nickname):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM recipients WHERE telegram_id=? AND nickname=?",
        (tg_id, nickname),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


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
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    cur.execute(
        """SELECT tx_hash, from_telegram_id, from_address, to_address, amount, token, memo 
        FROM transactions 
        WHERE notification_sent=0"""
    )
    
    pending = cur.fetchall()
    conn.close()
    
    for tx_hash, from_tg_id, from_addr, to_addr, amount, token, memo in pending:
        recipient_tg_id = get_telegram_id_by_address(to_addr)
        
        if recipient_tg_id and recipient_tg_id != from_tg_id:
            success = await send_payment_notification(
                recipient_tg_id, from_addr, amount, token, memo, tx_hash
            )
            
            if success:
                mark_notification_sent(tx_hash)
                await asyncio.sleep(1)


async def notification_worker(application):
    global bot_instance
    bot_instance = application.bot
    
    print("✓ Notification worker started")
    
    while True:
        try:
            await check_pending_notifications()
            await asyncio.sleep(30)  # Increased from 10s to reduce load
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
        addr = create_tempo_wallet(q.from_user.id)
        await q.message.reply_text(
            f"✅ <b>Tempo wallet created!</b>\n\n"
            f"<code>{addr}</code>\n\n"
            f"💡 Fund this wallet to start sending payments\n"
            f"🚰 Faucet: {TEMPO_FAUCET}\n\n"
            f"🔔 You'll receive notifications when someone sends you payment!",
            parse_mode="HTML"
        )
    else:
        try:
            native_balance = await get_balance(w[0])
            native_eth = w3.from_wei(native_balance, 'ether')
            
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"💰 <b>Balance:</b>\n"
                f"TEMO: {native_eth:.4f}\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 Get testnet tokens: {TEMPO_FAUCET}"
            )
            
            await q.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            msg = (
                f"👛 <b>Your Tempo Wallet</b>\n\n"
                f"<code>{w[0]}</code>\n\n"
                f"⚠️ <i>Could not fetch balance (RPC rate limited)</i>\n\n"
                f"🔔 Notifications: <b>Enabled</b>\n\n"
                f"🚰 Get testnet tokens: {TEMPO_FAUCET}"
            )
            await q.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    wallet = get_wallet(q.from_user.id)
    if not wallet:
        await q.message.reply_text("No wallet found")
        return
    
    user_address = wallet[0].lower()
    
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
    
    conn.close()
    
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
        keyboard = [[InlineKeyboardButton("➕ Add Recipient", callback_data="add_recipient")]]
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
    
    if not step:
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
            # Rate-limited RPC calls
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

            # Add memo to transaction data
            memo_bytes = memo.encode('utf-8')
            memo_hex = memo_bytes.hex()
            tx_dict["data"] = tx_dict["data"] + memo_hex

            # Sign and send transaction
            signed_tx = w3.eth.account.sign_transaction(tx_dict, acct.key)
            tx_hash = await send_raw_transaction(signed_tx.rawTransaction)
            
            tx_hash_hex = tx_hash.hex()
            explorer_url = f"https://explore.tempo.xyz/tx/{tx_hash_hex}"

            # Save transaction to database
            save_transaction(
                tx_hash_hex,
                update.effective_user.id,
                acct.address,
                to,
                str(amount),
                token,
                memo
            )

            # Display success message
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
            
            # Check for specific error types
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
    """Initialize background workers after app starts"""
    asyncio.create_task(notification_worker(application))


def main():
    """Main function to start the bot"""
    # Check and migrate old database if needed
    if os.path.exists(DB_FILE):
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA table_info(wallets)")
            columns = [row[1] for row in cur.fetchall()]
            if 'tempo_address' not in columns:
                raise sqlite3.OperationalError("Old schema")
            cur.execute("SELECT tempo_address FROM wallets LIMIT 1")
            conn.close()
        except sqlite3.OperationalError:
            conn.close()
            print("⚠️  Old database schema detected. Removing...")
            os.remove(DB_FILE)
            print("✓ Old database removed. Creating new one...")
    
    # Initialize database
    init_db()
    print("✓ Database initialized")

    # Build application
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(wallet_menu, pattern="^wallet$"))
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

    # Start bot
    print("=" * 50)
    print("🚀 Tempo Payment Bot with Notifications")
    print("=" * 50)
    print(f"✓ Tempo RPC: {TEMPO_RPC}")
    print(f"✓ Chain ID: {TEMPO_CHAIN_ID}")
    print(f"✓ Rate Limit: {RPC_CALL_DELAY}s between calls")
    print(f"✓ Max Retries: {MAX_RETRIES}")
    print(f"✓ Notification system: Active")
    print("=" * 50)
    print("Bot is running... Press Ctrl+C to stop")
    print("=" * 50)
    
    app.run_polling()


if __name__ == "__main__":
    main()