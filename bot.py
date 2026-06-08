"""
Beschaffungs-Bot für Telegram
Sammelt Bestellanfragen und speichert sie in Google Sheets

Features:
- Order numbers (#001, #002, etc.)
- View pending orders (/meine_bestellungen)
- Cancel orders (/stornieren)
- Admin notifications for new orders
- Search orders (/suche)
- Image attachments for orders
- Weekly summary (Mondays)
"""

import os
import json
import logging
from datetime import datetime, time, timedelta
from dotenv import load_dotenv

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============== CONFIGURATION ==============
# Roles:
# 0: User (Standard)
# 1: Admin (Besteller/Einkäufer - matches and confirms)
# 2: SuperAdmin (Full control - includes cancellation/deletion)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Admin IDs (Admins/Orderers) - can be comma-separated list
ADMIN_CHAT_IDS = [id.strip() for id in os.getenv("ADMIN_CHAT_ID", "").split(",") if id.strip()]

# Super-Admin IDs (Full control) - can be comma-separated list
SUPER_ADMIN_IDS = [id.strip() for id in os.getenv("SUPER_ADMIN_IDS", "").split(",") if id.strip()]

# Blocked Telegram user IDs - can be comma-separated list
BLOCKED_CHAT_IDS = [id.strip() for id in os.getenv("BLOCKED_CHAT_IDS", "").split(",") if id.strip()]

def get_user_role(chat_id: int | str) -> int:
    """Helper to determine the user role (0: User, 1: Admin, 2: SuperAdmin)."""
    chat_id_str = str(chat_id).strip()
    if chat_id_str in SUPER_ADMIN_IDS:
        return 2
    if chat_id_str in ADMIN_CHAT_IDS:
        return 1
    return 0

def is_any_admin(chat_id: int | str) -> bool:
    """Check if user has at least Admin role."""
    return get_user_role(chat_id) >= 1

def is_blocked_user(chat_id: int | str) -> bool:
    """Check if a Telegram user is blocked."""
    return get_user_role(chat_id) == 0 and str(chat_id).strip() in BLOCKED_CHAT_IDS

def format_id_list(ids: list[str]) -> str:
    """Format ID list for admin replies."""
    return ", ".join(ids) if ids else "keine"

# Google Sheets
GOOGLE_SHEET_ID = "1nb7A0nCucAwz2ylBrIl65OQ5J3LgbqHErS5nkrK2rH0"

# Urgency options
DRINGLICHKEIT_OPTIONS = [["🔴 Dringend", "🟢 Normal"]]

# Cost center options - CUSTOMIZE THESE FOR YOUR COMPANY!
KOSTENSTELLE_OPTIONS = [
    ["Lager", "Stahlhalle", "Bulli"],
    ["HR", "Finanzen", "Produktion"],
    ["Andere"]
]

# Conversation states
ARTIKEL, MENGE, DRINGLICHKEIT, KOSTENSTELLE, FOTO, BESTAETIGUNG, STORNO_AUSWAHL = range(7)


# ============== Google Sheets Functions ==============

def get_google_sheet():
    """Connect to Google Sheets using service account."""
    try:
        spreadsheet = get_google_spreadsheet()
        if not spreadsheet:
            return None

        return spreadsheet.sheet1
    except Exception as e:
        logger.error(f"Error connecting to Google Sheets: {type(e).__name__}: {e}")
        return None


def get_google_spreadsheet():
    """Connect to the Google spreadsheet using service account."""
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        # Try environment variable first (for cloud deployment)
        google_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        else:
            # Fall back to file (for local development)
            credentials = Credentials.from_service_account_file("credentials.json", scopes=scopes)

        client = gspread.authorize(credentials)
        return client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        logger.error(f"Error connecting to Google spreadsheet: {type(e).__name__}: {e}")
        return None


def get_user_sheet():
    """Get or create the user registry worksheet."""
    try:
        spreadsheet = get_google_spreadsheet()
        if not spreadsheet:
            return None

        try:
            return spreadsheet.worksheet("Benutzer")
        except WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="Benutzer", rows=1000, cols=8)
            worksheet.append_row(
                ["ChatId", "Vorname", "Nachname", "Username", "Rolle", "Blockiert", "Erster Start", "Letzter Start"],
                value_input_option="USER_ENTERED"
            )
            return worksheet
    except Exception as e:
        logger.error(f"Error getting user sheet: {e}")
        return None


def role_name_for_user(chat_id: int | str) -> str:
    """Return a readable role name."""
    role = get_user_role(chat_id)
    if role >= 2:
        return "Super-Admin"
    if role == 1:
        return "Admin"
    return "Benutzer"


def record_known_user(user) -> None:
    """Store or update a Telegram user in the user registry."""
    try:
        worksheet = get_user_sheet()
        if not worksheet:
            return

        chat_id = str(user.id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        all_values = worksheet.get_all_values()

        target_row = None
        for row_index, row in enumerate(all_values[1:], start=2):
            if row and row[0] == chat_id:
                target_row = row_index
                break

        row_data = [
            chat_id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            role_name_for_user(chat_id),
            "Ja" if is_blocked_user(chat_id) else "Nein",
            now,
            now,
        ]

        if target_row:
            first_seen = all_values[target_row - 1][6] if len(all_values[target_row - 1]) > 6 else now
            row_data[6] = first_seen
            worksheet.update(f"A{target_row}:H{target_row}", [row_data], value_input_option="USER_ENTERED")
        else:
            worksheet.append_row(row_data, value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error(f"Error recording known user: {e}")


def set_known_user_blocked_status(chat_id: int | str, blocked: bool) -> None:
    """Update blocked status in the user registry if the user is known."""
    try:
        worksheet = get_user_sheet()
        if not worksheet:
            return

        chat_id_str = str(chat_id)
        all_values = worksheet.get_all_values()
        for row_index, row in enumerate(all_values[1:], start=2):
            if row and row[0] == chat_id_str:
                worksheet.update_cell(row_index, 6, "Ja" if blocked else "Nein")
                worksheet.update_cell(row_index, 5, role_name_for_user(chat_id_str))
                return
    except Exception as e:
        logger.error(f"Error updating known user blocked status: {e}")


def get_known_users() -> list[dict]:
    """Return known users from the user registry and historic orders."""
    users: dict[str, dict] = {}

    try:
        user_sheet = get_user_sheet()
        if user_sheet:
            for row in user_sheet.get_all_values()[1:]:
                if not row or not row[0]:
                    continue

                chat_id = row[0]
                users[chat_id] = {
                    "chat_id": chat_id,
                    "first_name": row[1] if len(row) > 1 else "",
                    "last_name": row[2] if len(row) > 2 else "",
                    "username": row[3] if len(row) > 3 else "",
                    "role": role_name_for_user(chat_id),
                    "blocked": is_blocked_user(chat_id),
                    "first_seen": row[6] if len(row) > 6 else "",
                    "last_seen": row[7] if len(row) > 7 else "",
                    "source": "Benutzerliste",
                }
    except Exception as e:
        logger.error(f"Error reading known users registry: {e}")

    try:
        order_sheet = get_google_sheet()
        if order_sheet:
            for row in order_sheet.get_all_values()[1:]:
                if len(row) < 4 or not row[3]:
                    continue

                chat_id = row[3]
                if chat_id in users:
                    continue

                users[chat_id] = {
                    "chat_id": chat_id,
                    "first_name": row[2] if len(row) > 2 else "",
                    "last_name": "",
                    "username": "",
                    "role": role_name_for_user(chat_id),
                    "blocked": is_blocked_user(chat_id),
                    "first_seen": row[1] if len(row) > 1 else "",
                    "last_seen": row[1] if len(row) > 1 else "",
                    "source": "Bestellungen",
                }
    except Exception as e:
        logger.error(f"Error reading known users from orders: {e}")

    return sorted(users.values(), key=lambda item: item.get("last_seen") or "", reverse=True)


def search_known_users(search_term: str) -> list[dict]:
    """Search known users by id, name, username, role, or source."""
    term = search_term.lower().strip()
    if not term:
        return get_known_users()

    results = []
    for user in get_known_users():
        haystack = " ".join([
            user.get("chat_id", ""),
            user.get("first_name", ""),
            user.get("last_name", ""),
            user.get("username", ""),
            user.get("role", ""),
            user.get("source", ""),
        ]).lower()
        if term in haystack:
            results.append(user)
    return results


def format_known_user(user: dict) -> str:
    """Format a known user for Telegram messages."""
    username = f"@{user['username']}" if user.get("username") else "kein Username"
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "Unbekannt"
    blocked = "Ja" if user.get("blocked") else "Nein"
    return (
        f"ID: {user['chat_id']}\n"
        f"Name: {name} ({username})\n"
        f"Rolle: {user.get('role', 'Benutzer')} | Blockiert: {blocked}\n"
        f"Quelle: {user.get('source', '-')}\n"
        f"Letzter Start/Eintrag: {user.get('last_seen') or '-'}"
    )


def get_next_order_number() -> str:
    """Get the next order number based on existing rows."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return "#001"

        # Count rows (excluding header)
        all_values = worksheet.get_all_values()
        order_count = len(all_values)  # includes header, so this gives us next number

        return f"#{order_count:03d}"
    except Exception as e:
        logger.error(f"Error getting order number: {e}")
        return "#???"


def save_to_sheet(data: dict) -> tuple[bool, str]:
    """Save a procurement request to Google Sheets. Returns (success, order_number)."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return False, ""

        # Get next order number
        order_number = get_next_order_number()

        # Prepare row data matching the columns:
        # BestellNr | Timestamp | Mitarbeiter | ChatId | Artikel | Menge | Dringlichkeit | Kostenstelle | Bestellt? | Bestellt am | Foto-ID
        row = [
            order_number,
            data["timestamp"],
            data["mitarbeiter"],
            str(data["chat_id"]),
            data["artikel"],
            data["menge"],
            data["dringlichkeit"],
            data["kostenstelle"],
            "",  # Bestellt?
            "",   # Bestellt am
            data.get("foto_id", "")  # Column K: Foto-ID
        ]

        worksheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"Saved order {order_number} from {data['mitarbeiter']}: {data['artikel']}")
        return True, order_number

    except Exception as e:
        logger.error(f"Error saving to sheet: {e}")
        return False, ""


def get_pending_orders_for_user(chat_id: int) -> list:
    """Get all pending orders for a specific user."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return []

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:  # Only header or empty
            return []

        pending = []
        for i, row in enumerate(all_values[1:], start=2):  # Skip header, start row counting at 2
            if len(row) >= 9:
                # Check if ChatId matches and not yet ordered (Bestellt? is empty)
                row_chat_id = row[3] if len(row) > 3 else ""
                bestellt = row[8] if len(row) > 8 else ""

                if str(chat_id) == row_chat_id and bestellt.strip() == "":
                    pending.append({
                        "row": i,
                        "order_number": row[0],
                        "timestamp": row[1],
                        "artikel": row[4],
                        "menge": row[5],
                        "dringlichkeit": row[6],
                        "kostenstelle": row[7]
                    })

        return pending
    except Exception as e:
        logger.error(f"Error getting pending orders: {e}")
        return []


def get_all_pending_orders() -> list:
    """Get all orders that are not yet marked as ordered or cancelled."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return []

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return []

        pending = []
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) >= 9:
                bestellt = row[8].strip().upper()
                if bestellt == "":
                    pending.append({
                        "row": i,
                        "order_number": row[0],
                        "timestamp": row[1],
                        "mitarbeiter": row[2],
                        "artikel": row[4],
                        "menge": row[5],
                        "dringlichkeit": row[6],
                        "kostenstelle": row[7]
                    })

        return pending
    except Exception as e:
        logger.error(f"Error getting all pending orders: {e}")
        return []


def update_order_status(row_number: int, status: str) -> bool:
    """Update order status in column I and set timestamp in column J."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return False

        # Column I (9): Status, Column J (10): Timestamp
        worksheet.update_cell(row_number, 9, status)
        worksheet.update_cell(row_number, 10, datetime.now().strftime("%Y-%m-%d %H:%M"))

        return True
    except Exception as e:
        logger.error(f"Error updating order status: {e}")
        return False


def cancel_order(row_number: int) -> bool:
    """Cancel an order by marking it as 'STORNIERT'."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return False

        # Mark as cancelled in 'Bestellt?' column (column I = 9)
        worksheet.update_cell(row_number, 9, "STORNIERT")
        worksheet.update_cell(row_number, 10, datetime.now().strftime("%Y-%m-%d %H:%M"))

        return True
    except Exception as e:
        logger.error(f"Error cancelling order: {e}")
        return False


def search_orders(search_term: str) -> list:
    """Search for orders by article name."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return []

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return []

        results = []
        search_lower = search_term.lower()

        for i, row in enumerate(all_values[1:], start=2):
            if len(row) >= 8:
                artikel = row[4].lower() if len(row) > 4 else ""
                mitarbeiter = row[2].lower() if len(row) > 2 else ""
                kostenstelle = row[7].lower() if len(row) > 7 else ""

                if search_lower in artikel or search_lower in mitarbeiter or search_lower in kostenstelle:
                    results.append({
                        "row": i,
                        "order_number": row[0],
                        "timestamp": row[1],
                        "mitarbeiter": row[2],
                        "artikel": row[4],
                        "menge": row[5],
                        "dringlichkeit": row[6],
                        "kostenstelle": row[7],
                        "bestellt": row[8] if len(row) > 8 else ""
                    })

        return results[:10]  # Limit to 10 results
    except Exception as e:
        logger.error(f"Error searching orders: {e}")
        return []


def get_weekly_summary() -> dict:
    """Get order statistics for the current week."""
    try:
        worksheet = get_google_sheet()
        if not worksheet:
            return {}

        all_values = worksheet.get_all_values()
        if len(all_values) <= 1:
            return {"total": 0, "pending": 0, "ordered": 0, "cancelled": 0}

        # Get current week's start (Monday)
        today = datetime.now()
        week_start = today.replace(hour=0, minute=0, second=0) - timedelta(days=today.weekday())

        total = 0
        pending = 0
        ordered = 0
        cancelled = 0
        by_kostenstelle = {}

        for row in all_values[1:]:
            if len(row) >= 9:
                try:
                    order_date = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
                    if order_date >= week_start:
                        total += 1
                        status = row[8].strip().upper()

                        if status == "":
                            pending += 1
                        elif status == "STORNIERT":
                            cancelled += 1
                        else:
                            ordered += 1

                        ks = row[7]
                        by_kostenstelle[ks] = by_kostenstelle.get(ks, 0) + 1
                except:
                    pass

        return {
            "total": total,
            "pending": pending,
            "ordered": ordered,
            "cancelled": cancelled,
            "by_kostenstelle": by_kostenstelle
        }
    except Exception as e:
        logger.error(f"Error getting weekly summary: {e}")
        return {}


# ============== Telegram Bot Handlers ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask for the article."""
    user = update.effective_user
    record_known_user(user)

    await update.message.reply_text(
        f"👋 Hallo {user.first_name}!\n\n"
        f"Ich helfe dir, Bestellanfragen zu erfassen.\n\n"
        f"📦 **1/5: Welcher Artikel?**\n\n"
        f"(/abbrechen zum Beenden)",
        parse_mode="Markdown"
    )

    # Notify admins when a new user starts the bot
    if ADMIN_CHAT_IDS:
        for admin_id in ADMIN_CHAT_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"👤 **Neuer Benutzer:** {user.first_name} (@{user.username or 'kein Username'}) "
                        f"hat den Bot gestartet.\n\n"
                        f"Telegram-ID: `{user.id}`"
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚫 Benutzer blockieren", callback_data=f"block_user_{user.id}")]
                    ]),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Could not notify admin {admin_id} about new user: {e}")

    return ARTIKEL


async def artikel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the article and ask for quantity."""
    context.user_data["artikel"] = update.message.text

    await update.message.reply_text(
        f"✅ Artikel: *{update.message.text}*\n\n"
        f"🔢 **2/5: Welche Menge?**",
        parse_mode="Markdown"
    )

    return MENGE


async def menge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the quantity and ask for urgency."""
    context.user_data["menge"] = update.message.text

    reply_keyboard = DRINGLICHKEIT_OPTIONS

    await update.message.reply_text(
        f"✅ Menge: *{update.message.text}*\n\n"
        f"⏰ **3/5: Dringend oder normal?**",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard,
            one_time_keyboard=True,
            resize_keyboard=True
        ),
        parse_mode="Markdown"
    )

    return DRINGLICHKEIT


async def dringlichkeit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the urgency and ask for cost center."""
    context.user_data["dringlichkeit"] = update.message.text

    reply_keyboard = KOSTENSTELLE_OPTIONS

    await update.message.reply_text(
        f"✅ Dringlichkeit: *{update.message.text}*\n\n"
        f"💰 **4/5: Für welche Kostenstelle ist die Bestellung?**",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard,
            one_time_keyboard=True,
            resize_keyboard=True
        ),
        parse_mode="Markdown"
    )

    return KOSTENSTELLE


async def kostenstelle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store cost center and ask for optional photo."""
    context.user_data["kostenstelle"] = update.message.text

    await update.message.reply_text(
        f"✅ Kostenstelle: *{update.message.text}*\n\n"
        f"📸 **5/5: Möchtest du ein Foto anhängen?**\n\n"
        f"Sende ein Foto oder tippe /weiter um ohne Foto fortzufahren.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

    return FOTO


async def foto_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle received photo."""
    if update.message.photo:
        # Get the largest photo
        photo = update.message.photo[-1]
        context.user_data["foto_id"] = photo.file_id
        await update.message.reply_text("📸 Foto erhalten!")

    return await show_confirmation(update, context)


async def foto_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip photo and show confirmation."""
    context.user_data["foto_id"] = ""
    return await show_confirmation(update, context)


async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show order summary and ask for confirmation."""
    user = update.effective_user

    foto_text = "\n📸 Foto: Ja" if context.user_data.get("foto_id") else ""

    keyboard = [
        [InlineKeyboardButton("✅ Bestätigen & Absenden", callback_data="confirm_yes")],
        [InlineKeyboardButton("✏️ Nochmal von vorne", callback_data="confirm_restart")],
        [InlineKeyboardButton("❌ Abbrechen", callback_data="confirm_cancel")]
    ]

    await update.message.reply_text(
        f"📋 **Bestellungsübersicht:**\n\n"
        f"📦 Artikel: *{context.user_data['artikel']}*\n"
        f"🔢 Menge: *{context.user_data['menge']}*\n"
        f"⏰ Dringlichkeit: *{context.user_data['dringlichkeit']}*\n"
        f"💰 Kostenstelle: *{context.user_data['kostenstelle']}*{foto_text}\n\n"
        f"❓ **Ist alles richtig?**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

    return BESTAETIGUNG


async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle confirmation button press."""
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_yes":
        # Save the order
        await query.edit_message_text("⏳ Bestellung wird gespeichert...")
        return await save_order(query, context, from_callback=True)

    elif query.data == "confirm_restart":
        await query.edit_message_text("🔄 Okay, lass uns nochmal von vorne anfangen!")
        context.user_data.clear()
        # Send new start message
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"👋 Neue Bestellung:\n\n"
                 f"📦 **1/5: Welcher Artikel?**\n\n"
                 f"(/abbrechen zum Beenden)",
            parse_mode="Markdown"
        )
        return ARTIKEL

    else:  # confirm_cancel
        await query.edit_message_text("❌ Bestellung abgebrochen.\n\n/start - Neue Bestellung")
        context.user_data.clear()
        return ConversationHandler.END


async def save_order(update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> int:
    """Save the complete order to Google Sheets."""
    # Determine how to send messages based on source
    if from_callback:
        # update is a CallbackQuery
        chat_id = update.message.chat_id
        user = update.from_user

        async def send_message(text, **kwargs):
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    else:
        chat_id = update.effective_chat.id
        user = update.effective_user

        async def send_message(text, **kwargs):
            await update.message.reply_text(text, **kwargs)

    # Prepare data for saving
    data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mitarbeiter": f"{user.first_name} {user.last_name or ''}".strip(),
        "chat_id": chat_id,
        "artikel": context.user_data["artikel"],
        "menge": context.user_data["menge"],
        "dringlichkeit": context.user_data["dringlichkeit"],
        "kostenstelle": context.user_data["kostenstelle"],
        "foto_id": context.user_data.get("foto_id", ""),
    }

    # Save to Google Sheets
    success, order_number = save_to_sheet(data)

    if success:
        foto_text = "\n📸 Mit Foto" if data["foto_id"] else ""
        await send_message(
            f"✅ Bestellanfrage {order_number} erfasst!\n\n"
            f"📦 Artikel: {data['artikel']}\n"
            f"🔢 Menge: {data['menge']}\n"
            f"⏰ Dringlichkeit: {data['dringlichkeit']}\n"
            f"💰 Kostenstelle: {data['kostenstelle']}{foto_text}\n\n"
            f"Du wirst benachrichtigt, wenn bestellt wurde.\n\n"
            f"📋 /meine_bestellungen - Deine offenen Bestellungen\n"
            f"🆕 /start - Neue Anfrage"
        )

        # Notify admins if configured
        if ADMIN_CHAT_IDS:
            for admin_id in ADMIN_CHAT_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🆕 Neue Bestellung {order_number}\n\n"
                             f"👤 Von: {data['mitarbeiter']}\n"
                             f"📦 Artikel: {data['artikel']}\n"
                             f"🔢 Menge: {data['menge']}\n"
                             f"⏰ Dringlichkeit: {data['dringlichkeit']}\n"
                             f"💰 Kostenstelle: {data['kostenstelle']}"
                    )

                    # Send photo to admin if available
                    if data["foto_id"]:
                        await context.bot.send_photo(
                            chat_id=admin_id,
                            photo=data["foto_id"],
                            caption=f"📸 Foto für Bestellung {order_number}"
                        )
                except Exception as e:
                    logger.error(f"Could not notify admin {admin_id}: {e}")
    else:
        await send_message(
            f"❌ Fehler beim Speichern!\n\n"
            f"Bitte versuche es später erneut oder kontaktiere den Administrator.\n\n"
            f"Für eine neue Anfrage: /start"
        )

    # Clear user data
    context.user_data.clear()

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "❌ Anfrage abgebrochen.\n\n"
        "Für eine neue Anfrage: /start",
        reply_markup=ReplyKeyboardRemove()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def meine_bestellungen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's pending orders."""
    chat_id = update.effective_chat.id
    pending = get_pending_orders_for_user(chat_id)

    if not pending:
        await update.message.reply_text(
            "📋 Du hast keine offenen Bestellungen.\n\n"
            "/start - Neue Bestellung aufgeben"
        )
        return

    message = "📋 **Deine offenen Bestellungen:**\n\n"
    for order in pending:
        message += (
            f"**{order['order_number']}** - {order['artikel']}\n"
            f"   Menge: {order['menge']} | {order['dringlichkeit']}\n"
            f"   Kostenstelle: {order['kostenstelle']}\n"
            f"   Datum: {order['timestamp']}\n\n"
        )

    message += "/stornieren - Bestellung stornieren\n"
    message += "/start - Neue Bestellung"

    await update.message.reply_text(message, parse_mode="Markdown")


async def stornieren_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the cancellation process - show pending orders."""
    chat_id = update.effective_chat.id
    pending = get_pending_orders_for_user(chat_id)

    if not pending:
        await update.message.reply_text(
            "📋 Du hast keine offenen Bestellungen zum Stornieren.\n\n"
            "/start - Neue Bestellung aufgeben"
        )
        return ConversationHandler.END

    # Store pending orders in context for later
    context.user_data["pending_orders"] = pending

    # Create inline keyboard with order options
    keyboard = []
    for order in pending:
        keyboard.append([InlineKeyboardButton(
            f"{order['order_number']} - {order['artikel']}",
            callback_data=f"cancel_{order['row']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel_abort")])

    await update.message.reply_text(
        "🗑️ **Welche Bestellung möchtest du stornieren?**\n\n"
        "Wähle eine Bestellung:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

    return STORNO_AUSWAHL


async def stornieren_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the cancellation selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_abort":
        await query.edit_message_text("❌ Stornierung abgebrochen.")
        return ConversationHandler.END

    # Extract row number from callback data
    row_number = int(query.data.replace("cancel_", ""))

    # Find the order details
    pending = context.user_data.get("pending_orders", [])
    order = next((o for o in pending if o["row"] == row_number), None)

    if order and cancel_order(row_number):
        await query.edit_message_text(
            f"✅ **Bestellung {order['order_number']} wurde storniert.**\n\n"
            f"📦 {order['artikel']} x {order['menge']}\n\n"
            f"/meine_bestellungen - Offene Bestellungen\n"
            f"/start - Neue Bestellung",
            parse_mode="Markdown"
        )

        # Notify admins
        if ADMIN_CHAT_IDS:
            for admin_id in ADMIN_CHAT_IDS:
                try:
                    user = update.effective_user
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🗑️ **Bestellung {order['order_number']} STORNIERT**\n\n"
                             f"👤 Von: {user.first_name}\n"
                             f"📦 Artikel: {order['artikel']}\n"
                             f"   Menge: {order['menge']}",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Could not notify admin {admin_id}: {e}")
    else:
        await query.edit_message_text(
            "❌ Fehler beim Stornieren. Bitte versuche es später erneut."
        )

    context.user_data.clear()
    return ConversationHandler.END


async def get_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper command to get your chat ID for admin setup."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"🔑 **Deine Chat-ID:** `{chat_id}`\n\n"
        f"Füge diese in die .env Datei ein:\n"
        f"`ADMIN_CHAT_ID={chat_id}` (für Einkäufer)\n"
        f"oder\n"
        f"`SUPER_ADMIN_IDS={chat_id}` (für volle Rechte)",
        parse_mode="Markdown"
    )

async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dynamically add an admin ID (SuperAdmin only)."""
    user_id = update.effective_user.id
    if get_user_role(user_id) < 2:
        await update.message.reply_text("⛔ Nur Super-Admins können Rechte vergeben.")
        return

    if not context.args:
        await update.message.reply_text("Verwendung: `/admin_add [ID]`", parse_mode="Markdown")
        return

    new_id = context.args[0].strip()
    if new_id not in ADMIN_CHAT_IDS:
        ADMIN_CHAT_IDS.append(new_id)
        await update.message.reply_text(f"✅ ID `{new_id}` wurde als Einkäufer hinzugefügt.\n\n*Hinweis:* Diese Änderung gilt nur bis zum nächsten Neustart des Bots. Bitte trage die ID dauerhaft in die .env Datei oder die Railway-Variablen unter `ADMIN_CHAT_ID` ein.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ ID `{new_id}` ist bereits als Admin hinterlegt.", parse_mode="Markdown")


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block a Telegram user ID (Admin only)."""
    user_id = update.effective_user.id
    if not is_any_admin(user_id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    if not context.args:
        await update.message.reply_text("Verwendung: `/block [Telegram-ID]`", parse_mode="Markdown")
        return

    blocked_id = context.args[0].strip()
    if is_any_admin(blocked_id):
        await update.message.reply_text("⛔ Admins und Super-Admins können nicht blockiert werden.")
        return

    if blocked_id not in BLOCKED_CHAT_IDS:
        BLOCKED_CHAT_IDS.append(blocked_id)
        set_known_user_blocked_status(blocked_id, True)
        await update.message.reply_text(
            f"✅ ID `{blocked_id}` wurde blockiert.\n\n"
            f"*Wichtig:* Dauerhaft bleibt die Sperre nur, wenn du in Railway die Variable "
            f"`BLOCKED_CHAT_IDS={format_id_list(BLOCKED_CHAT_IDS)}` setzt.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"ℹ️ ID `{blocked_id}` ist bereits blockiert.", parse_mode="Markdown")


async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unblock a Telegram user ID (Admin only)."""
    user_id = update.effective_user.id
    if not is_any_admin(user_id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    if not context.args:
        await update.message.reply_text("Verwendung: `/unblock [Telegram-ID]`", parse_mode="Markdown")
        return

    blocked_id = context.args[0].strip()
    if blocked_id in BLOCKED_CHAT_IDS:
        BLOCKED_CHAT_IDS.remove(blocked_id)
        set_known_user_blocked_status(blocked_id, False)
        await update.message.reply_text(
            f"✅ ID `{blocked_id}` wurde entsperrt.\n\n"
            f"Aktuelle Railway-Variable: `BLOCKED_CHAT_IDS={format_id_list(BLOCKED_CHAT_IDS)}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"ℹ️ ID `{blocked_id}` ist nicht blockiert.", parse_mode="Markdown")


async def blocked_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show blocked Telegram user IDs (Admin only)."""
    if not is_any_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    await update.message.reply_text(
        f"🚫 **Blockierte IDs:** `{format_id_list(BLOCKED_CHAT_IDS)}`",
        parse_mode="Markdown"
    )


def user_action_keyboard(user: dict) -> InlineKeyboardMarkup | None:
    """Build block/unblock action keyboard for a known user."""
    if is_any_admin(user["chat_id"]):
        return None

    if user.get("blocked"):
        button = InlineKeyboardButton("✅ Entsperren", callback_data=f"unblock_user_{user['chat_id']}")
    else:
        button = InlineKeyboardButton("🚫 Blockieren", callback_data=f"block_user_{user['chat_id']}")
    return InlineKeyboardMarkup([[button]])


async def send_known_users(update: Update, users: list[dict], title: str) -> None:
    """Send known users as separate Telegram messages with action buttons."""
    if not users:
        await update.message.reply_text("Keine Benutzer gefunden.")
        return

    shown_users = users[:20]
    await update.message.reply_text(f"{title}\nGefunden: {len(users)}. Angezeigt: {len(shown_users)}.")
    for user in shown_users:
        await update.message.reply_text(
            format_known_user(user),
            reply_markup=user_action_keyboard(user)
        )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show known users (Admin only)."""
    if not is_any_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    await send_known_users(update, get_known_users(), "👥 Bekannte Benutzer")


async def users_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search known users (Admin only)."""
    if not is_any_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    if not context.args:
        await update.message.reply_text("Verwendung: /benutzer_suche [Name, Username oder ID]")
        return

    search_term = " ".join(context.args)
    await send_known_users(update, search_known_users(search_term), f"🔍 Benutzer-Suche: {search_term}")


async def block_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin block button from new-user notification."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not is_any_admin(user_id):
        await query.answer("⛔ Nicht autorisiert.")
        return

    blocked_id = query.data.replace("block_user_", "", 1).strip()
    if is_any_admin(blocked_id):
        await query.answer("⛔ Admins können nicht blockiert werden.")
        return

    if blocked_id not in BLOCKED_CHAT_IDS:
        BLOCKED_CHAT_IDS.append(blocked_id)
    set_known_user_blocked_status(blocked_id, True)

    await query.answer("Benutzer blockiert.")
    await query.edit_message_text(
        f"{query.message.text}\n\n"
        f"🚫 Blockiert durch {update.effective_user.first_name}.\n"
        f"Railway dauerhaft setzen: BLOCKED_CHAT_IDS={format_id_list(BLOCKED_CHAT_IDS)}"
    )


async def unblock_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin unblock button from user list."""
    query = update.callback_query
    user_id = update.effective_user.id

    if not is_any_admin(user_id):
        await query.answer("⛔ Nicht autorisiert.")
        return

    blocked_id = query.data.replace("unblock_user_", "", 1).strip()
    if blocked_id in BLOCKED_CHAT_IDS:
        BLOCKED_CHAT_IDS.remove(blocked_id)
    set_known_user_blocked_status(blocked_id, False)

    await query.answer("Benutzer entsperrt.")
    await query.edit_message_text(
        f"{query.message.text}\n\n"
        f"✅ Entsperrt durch {update.effective_user.first_name}.\n"
        f"Railway dauerhaft setzen: BLOCKED_CHAT_IDS={format_id_list(BLOCKED_CHAT_IDS)}"
    )


async def blocked_message_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop blocked users before normal message handlers run."""
    if update.effective_user and is_blocked_user(update.effective_user.id):
        if update.message:
            await update.message.reply_text("⛔ Du bist für diesen Bot blockiert.")
        raise ApplicationHandlerStop


async def blocked_callback_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop blocked users before normal callback handlers run."""
    if update.effective_user and is_blocked_user(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ Blockiert.", show_alert=True)
        raise ApplicationHandlerStop


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple test command to see if bot is alive."""
    await update.message.reply_text("🤖 Bot ist online! Wenn du das hier siehst, reagiert der Bot auf Befehle.")


async def admin_bestellungen_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all pending orders for admin management."""
    try:
        user_id = update.effective_user.id
        role = get_user_role(user_id)

        if role == 0:
            await update.message.reply_text(f"⛔ Nur für Admins. (Deine ID: {user_id})")
            return

        pending = get_all_pending_orders()

        if not pending:
            await update.message.reply_text("📋 Es liegen aktuell keine offenen Bestellungen vor.")
            return

        await update.message.reply_text(f"📋 **{len(pending)} offene Bestellungen:**")

        for order in pending:
            # First row of buttons: Bestellt, Angekommen (Available to all admins)
            buttons = [
                InlineKeyboardButton("✅ Bestellt", callback_data=f"status_{order['row']}_BESTELLT"),
                InlineKeyboardButton("📦 Angekommen", callback_data=f"status_{order['row']}_ERHALTEN")
            ]

            keyboard = [buttons]

            # Second row: Stornieren (Only available to SuperAdmins)
            if role >= 2:
                keyboard.append([InlineKeyboardButton("❌ Stornieren", callback_data=f"status_{order['row']}_STORNIERT")])

            text = (
                f"🆔 **{order['order_number']}**\n"
                f"👤 Von: {order['mitarbeiter']}\n"
                f"📦 Artikel: *{order['artikel']}*\n"
                f"🔢 Menge: {order['menge']}\n"
                f"💰 Kostenstelle: {order['kostenstelle']}\n"
                f"⏰ Dringlichkeit: {order['dringlichkeit']}\n"
                f"📅 Datum: {order['timestamp']}"
            )

            await update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error in admin_bestellungen_command: {e}")
        await update.message.reply_text(f"❌ Fehler: {e}")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """General admin menu shortcut."""
    try:
        user_id = update.effective_user.id
        role = get_user_role(user_id)

        if role == 0:
            await update.message.reply_text(f"⛔ Nur für Admins. (ID: {user_id})")
            return

        role_name = "Super-Admin" if role >= 2 else "Einkäufer/Admin"

        await update.message.reply_text(
            f"👑 **Admin Menü ({role_name})**\n\n"
            "Verfügbare Befehle:\n"
            "/admin_bestellungen - Offene Bestellungen verwalten\n"
            "/benutzer - Bekannte Benutzer anzeigen\n"
            "/benutzer_suche [Begriff] - Benutzer suchen\n"
            "/statistik - Wochenstatistik\n"
            "/meine_id - Deine Chat-ID prüfen",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in admin_command: {e}")
        await update.message.reply_text(f"❌ Fehler: {e}")


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status update button press by admin."""
    query = update.callback_query
    user_id = update.effective_user.id
    role = get_user_role(user_id)

    # Secure check: ensure user has at least Admin role
    if role == 0:
        await query.answer("⛔ Nicht autorisiert.")
        return

    # Extract data: status_ROW_NEWSTATUS
    parts = query.data.split("_")
    row_number = int(parts[1])
    new_status = parts[2]

    # Secure check: only SuperAdmin (role 2) can stornieren
    if new_status == "STORNIERT" and role < 2:
        await query.answer("⛔ Nur Super-Admins dürfen stornieren.")
        return

    await query.answer()

    if update_order_status(row_number, new_status):
        status_text = "✅ Bestellt" if new_status == "BESTELLT" else "📦 Angekommen" if new_status == "ERHALTEN" else "❌ Storniert"
        await query.edit_message_text(
            f"{query.message.text}\n\n"
            f"UPDATE: {status_text} am {datetime.now().strftime('%d.%m. %H:%M')} (von {update.effective_user.first_name})"
        )
    else:
        await query.message.reply_text("❌ Fehler beim Aktualisieren des Status.")


async def einladen_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provide bot link for inviting others."""
    bot_link = f"https://t.me/{(await context.bot.get_me()).username}"

    keyboard = [
        [InlineKeyboardButton("📨 Bot teilen", url=f"https://t.me/share/url?url={bot_link}&text=Hier ist der Beschaffungs-Bot für unsere Bestellungen!")],
    ]

    await update.message.reply_text(
        f"🤝 **Leute einladen**\n\n"
        f"Teile diesen Link mit deinen Kollegen, damit sie auch Bestellanfragen stellen können:\n\n"
        f"{bot_link}\n\n"
        f"Oder klicke auf den Button unten, um den Bot direkt in Telegram zu teilen.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    user_id = update.effective_user.id
    role = get_user_role(user_id)

    help_text = (
        "🤖 **Beschaffungs-Bot Hilfe**\n\n"
        "**Befehle:**\n"
        "/start - Neue Bestellanfrage starten\n"
        "/meine_bestellungen - Offene Bestellungen anzeigen\n"
        "/stornieren - Eigene Bestellung stornieren\n"
        "/einladen - Kollegen einladen\n"
        "/abbrechen - Aktuelle Anfrage abbrechen\n"
        "/meine_id - Deine Chat-ID anzeigen\n"
        "/hilfe - Diese Hilfe anzeigen\n\n"
    )

    # Add admin commands to help if user is admin
    if role >= 1:
        role_name = "Super-Admin" if role >= 2 else "Einkäufer"
        help_text += (
            f"👑 **{role_name}-Befehle:**\n"
            "/admin_bestellungen - Offene Bestellungen verwalten\n"
            "/suche [Begriff] - In allen Bestellungen suchen\n"
            "/benutzer - Bekannte Benutzer anzeigen\n"
            "/benutzer_suche [Begriff] - Benutzer suchen\n"
            "/statistik - Wochenstatistik anzeigen\n"
            "/block [ID] - Benutzer blockieren\n"
            "/unblock [ID] - Benutzer entsperren\n"
            "/blockierte - Blockierte IDs anzeigen\n"
            "/admin - Admin-Hauptmenü\n\n"
        )

    help_text += "Bei Problemen kontaktiere deinen Administrator."

    await update.message.reply_text(help_text, parse_mode="Markdown")


async def suche_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search for orders (Admin only)."""
    if not is_any_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    if not context.args:
        await update.message.reply_text(
            "🔍 **Bestellungen suchen**\n\n"
            "Verwendung: `/suche Suchbegriff`\n\n"
            "Beispiele:\n"
            "- `/suche Druckerpapier`\n"
            "- `/suche IT`\n"
            "- `/suche Max`",
            parse_mode="Markdown"
        )
        return

    search_term = " ".join(context.args)
    results = search_orders(search_term)

    if not results:
        await update.message.reply_text(
            f"🔍 Keine Ergebnisse für *{search_term}*\n\n"
            f"Versuche einen anderen Suchbegriff.",
            parse_mode="Markdown"
        )
        return

    message = f"🔍 **Suchergebnisse für '{search_term}':**\n\n"
    for order in results:
        status = "✅" if order['bestellt'] and order['bestellt'] != "STORNIERT" else "❌" if order['bestellt'] == "STORNIERT" else "⏳"
        message += (
            f"{status} **{order['order_number']}** - {order['artikel']}\n"
            f"   {order['mitarbeiter']} | {order['menge']} | {order['kostenstelle']}\n"
            f"   {order['timestamp']}\n\n"
        )

    await update.message.reply_text(message, parse_mode="Markdown")


async def statistik_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show weekly statistics (Admin only)."""
    if not is_any_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Nur für Administratoren.")
        return

    stats = get_weekly_summary()

    if not stats:
        await update.message.reply_text("Fehler beim Laden der Statistik.")
        return

    message = "📊 **Wochenübersicht**\n\n"
    message += f"📦 Gesamt: {stats.get('total', 0)} Bestellungen\n"
    message += f"⏳ Offen: {stats.get('pending', 0)}\n"
    message += f"✅ Bestellt: {stats.get('ordered', 0)}\n"
    message += f"❌ Storniert: {stats.get('cancelled', 0)}\n\n"

    if stats.get('by_kostenstelle'):
        message += "**Nach Kostenstelle:**\n"
        for ks, count in stats['by_kostenstelle'].items():
            message += f"  {ks}: {count}\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send weekly summary to all admins (scheduled job)."""
    if not ADMIN_CHAT_IDS:
        return

    stats = get_weekly_summary()
    if not stats:
        return

    message = "📅 **Wöchentliche Zusammenfassung**\n\n"
    message += f"📦 Gesamt: {stats.get('total', 0)} Bestellungen\n"
    message += f"⏳ Offen: {stats.get('pending', 0)}\n"
    message += f"✅ Bestellt: {stats.get('ordered', 0)}\n"
    message += f"❌ Storniert: {stats.get('cancelled', 0)}\n\n"

    if stats.get('by_kostenstelle'):
        message += "**Nach Kostenstelle:**\n"
        for ks, count in stats['by_kostenstelle'].items():
            message += f"  {ks}: {count}\n"

    for admin_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Could not send weekly summary to {admin_id}: {e}")


async def post_init(application: Application) -> None:
    """Register bot commands for the dropdown menu."""
    commands = [
        BotCommand("start", "Neue Bestellung starten"),
        BotCommand("meine_bestellungen", "Meine offenen Anfragen"),
        BotCommand("stornieren", "Bestellung stornieren"),
        BotCommand("einladen", "Kollegen einladen"),
        BotCommand("hilfe", "Hilfe anzeigen"),
    ]
    await application.bot.set_my_commands(commands)

    # Register admin commands for each configured admin/super-admin chat.
    admin_command_ids = list(dict.fromkeys(ADMIN_CHAT_IDS + SUPER_ADMIN_IDS))
    for admin_id in admin_command_ids:
        try:
            admin_commands = commands + [
                BotCommand("admin", "Admin-Menü öffnen"),
                BotCommand("admin_bestellungen", "Alle offenen Bestellungen verwalten"),
                BotCommand("suche", "Bestellungen suchen"),
                BotCommand("benutzer", "Bekannte Benutzer anzeigen"),
                BotCommand("benutzer_suche", "Benutzer suchen"),
                BotCommand("statistik", "Wochenstatistik anzeigen"),
                BotCommand("block", "Benutzer blockieren"),
                BotCommand("unblock", "Benutzer entsperren"),
                BotCommand("blockierte", "Blockierte IDs anzeigen"),
            ]
            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=int(admin_id))
            )
            logger.info(f"👑 Admin commands registered for {admin_id}")
        except Exception as e:
            logger.error(f"Could not register admin commands for {admin_id}: {e}")

    logger.info("✅ Slash commands registered in Telegram menu.")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set! Check your .env file.")
        return

    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Order conversation handler
    order_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ARTIKEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, artikel)],
            MENGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, menge)],
            DRINGLICHKEIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dringlichkeit)],
            KOSTENSTELLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, kostenstelle)],
            FOTO: [
                MessageHandler(filters.PHOTO, foto_received),
                CommandHandler("weiter", foto_skip),
                CommandHandler("skip", foto_skip),
            ],
            BESTAETIGUNG: [
                CallbackQueryHandler(confirmation_callback, pattern="^confirm_")
            ],
        },
        fallbacks=[
            CommandHandler("abbrechen", cancel),
            CommandHandler("cancel", cancel),
        ],
    )

    # Cancel order conversation handler
    storno_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("stornieren", stornieren_start)],
        states={
            STORNO_AUSWAHL: [CallbackQueryHandler(stornieren_callback)],
        },
        fallbacks=[
            CommandHandler("abbrechen", cancel),
        ],
    )

    # Blocked users are stopped before regular commands/conversations run.
    application.add_handler(MessageHandler(filters.ALL, blocked_message_guard), group=-1)
    application.add_handler(CallbackQueryHandler(blocked_callback_guard), group=-1)

    # Add handlers
    application.add_handler(order_conv_handler)
    application.add_handler(storno_conv_handler)
    application.add_handler(CommandHandler("meine_bestellungen", meine_bestellungen))
    application.add_handler(CommandHandler("bestellungen", meine_bestellungen))
    application.add_handler(CommandHandler("suche", suche_command))
    application.add_handler(CommandHandler("statistik", statistik_command))
    application.add_handler(CommandHandler("meine_id", get_my_id))
    application.add_handler(CommandHandler("einladen", einladen_command))
    application.add_handler(CommandHandler("invite", einladen_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("admin_bestellungen", admin_bestellungen_command))
    application.add_handler(CommandHandler("bestellungen_admin", admin_bestellungen_command))
    application.add_handler(CommandHandler("admin_bestellung", admin_bestellungen_command))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CallbackQueryHandler(block_user_callback, pattern="^block_user_"))
    application.add_handler(CallbackQueryHandler(unblock_user_callback, pattern="^unblock_user_"))
    application.add_handler(CallbackQueryHandler(status_callback, pattern="^status_"))
    application.add_handler(CommandHandler("hilfe", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin_add", admin_add_command))
    application.add_handler(CommandHandler("block", block_command))
    application.add_handler(CommandHandler("unblock", unblock_command))
    application.add_handler(CommandHandler("blockierte", blocked_list_command))
    application.add_handler(CommandHandler("benutzer", users_command))
    application.add_handler(CommandHandler("benutzer_suche", users_search_command))

    # Weekly summary: Use /statistik command manually
    # Automatic scheduling requires 24/7 hosting

    # Start the bot
    logger.info("🚀 Bot is starting...")
    logger.info(f"⚙️ Geladene Admins: {len(ADMIN_CHAT_IDS)}")
    logger.info(f"⚙️ Geladene Super-Admins: {len(SUPER_ADMIN_IDS)}")
    if ADMIN_CHAT_IDS:
        logger.info(f"📢 Admin notifications enabled for: {', '.join(ADMIN_CHAT_IDS)}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
