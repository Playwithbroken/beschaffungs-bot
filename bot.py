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
from google.oauth2.service_account import Credentials

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
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
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Optional: Get notified of new orders

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
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = spreadsheet.sheet1
        
        return worksheet
    except Exception as e:
        logger.error(f"Error connecting to Google Sheets: {type(e).__name__}: {e}")
        return None


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
        # BestellNr | Timestamp | Mitarbeiter | ChatId | Artikel | Menge | Dringlichkeit | Kostenstelle | Bestellt? | Bestellt am
        row = [
            order_number,
            data["timestamp"],
            data["mitarbeiter"],
            str(data["chat_id"]),
            data["artikel"],
            data["menge"],
            data["dringlichkeit"],
            data["kostenstelle"],
            "",  # Bestellt? - to be filled manually
            ""   # Bestellt am - to be filled manually
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
    
    await update.message.reply_text(
        f"👋 Hallo {user.first_name}!\n\n"
        f"Ich helfe dir, Bestellanfragen zu erfassen.\n\n"
        f"📦 **1/5: Welcher Artikel?**\n\n"
        f"(/abbrechen zum Beenden)",
        parse_mode="Markdown"
    )
    
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
        
        # Notify admin if configured
        if ADMIN_CHAT_ID:
            try:
                admin_msg = await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
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
                        chat_id=ADMIN_CHAT_ID,
                        photo=data["foto_id"],
                        caption=f"📸 Foto für Bestellung {order_number}"
                    )
            except Exception as e:
                logger.error(f"Could not notify admin: {e}")
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
        
        # Notify admin
        if ADMIN_CHAT_ID:
            try:
                user = update.effective_user
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"🗑️ **Bestellung {order['order_number']} STORNIERT**\n\n"
                         f"👤 Von: {user.first_name}\n"
                         f"📦 Artikel: {order['artikel']}\n"
                         f"🔢 Menge: {order['menge']}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Could not notify admin: {e}")
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
        f"`ADMIN_CHAT_ID={chat_id}`",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    await update.message.reply_text(
        "🤖 **Beschaffungs-Bot Hilfe**\n\n"
        "**Befehle:**\n"
        "/start - Neue Bestellanfrage starten\n"
        "/meine_bestellungen - Offene Bestellungen anzeigen\n"
        "/stornieren - Bestellung stornieren\n"
        "/suche [Begriff] - Bestellungen suchen\n"
        "/statistik - Wochenübersicht\n"
        "/abbrechen - Aktuelle Anfrage abbrechen\n"
        "/meine_id - Deine Chat-ID anzeigen\n"
        "/hilfe - Diese Hilfe anzeigen\n\n"
        "Bei Problemen kontaktiere deinen Administrator.",
        parse_mode="Markdown"
    )


async def suche_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search for orders."""
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
    """Show weekly statistics."""
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
    """Send weekly summary to admin (scheduled job)."""
    if not ADMIN_CHAT_ID:
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
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Could not send weekly summary: {e}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set! Check your .env file.")
        return
    
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
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
    
    # Add handlers
    application.add_handler(order_conv_handler)
    application.add_handler(storno_conv_handler)
    application.add_handler(CommandHandler("meine_bestellungen", meine_bestellungen))
    application.add_handler(CommandHandler("bestellungen", meine_bestellungen))
    application.add_handler(CommandHandler("suche", suche_command))
    application.add_handler(CommandHandler("statistik", statistik_command))
    application.add_handler(CommandHandler("meine_id", get_my_id))
    application.add_handler(CommandHandler("hilfe", help_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Weekly summary: Use /statistik command manually
    # Automatic scheduling requires 24/7 hosting
    
    # Start the bot
    logger.info("🚀 Bot is starting...")
    if ADMIN_CHAT_ID:
        logger.info(f"📢 Admin notifications enabled for chat ID: {ADMIN_CHAT_ID}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
