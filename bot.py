import os
import json
import logging
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация ИИ-клиента Claude с моделью 4.5 Sonnet
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SPREADSHEET_ID = "1NNb7CeNl9gU5TXbJTGvx5RsMMvxgPY39J4nWPFSprBl"

def get_credentials():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

def get_sheet_data(sheet_name):
    try:
        creds = get_credentials()
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.worksheet(sheet_name)
        data = sheet.get_all_values()
        logger.info(f"SUCCESS: Loaded {len(data)} rows from '{sheet_name}'")
        return data
    except Exception as e:
        logger.error(f"Sheets read error ({sheet_name}): {e}")
        return None

def update_cell_direct(sheet_name, row_number, col_number, value):
    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds)
        
        col_letter = chr(64 + col_number)
        range_name = f"{sheet_name}!{col_letter}{row_number}"
        body = {"values": [[str(value)]]}
        
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption="RAW",
            body=body
        ).execute()
        logger.info(f"Updated {range_name} = {value}")
        return True
    except Exception as e:
        logger.error(f"Direct update error: {e}")
        return False

def update_lumber_stock(message_text, qty_change):
    data = get_sheet_data("LUMBER")
    if not data:
        return "ERROR_CONN|Could not connect to database."

    words_to_remove = ['took', 'added', 'minus', 'plus', 'of', 'from',
                       'warehouse', 'stock', 'to', 'the', 'items', 'pieces', 'pcs',
                       'received', 'used', 'pulled', 'got', 'i', 'we']
    clean_text = message_text.lower()
    for w in words_to_remove:
        clean_text = re.sub(r'\b' + w + r'\b', '', clean_text)
    clean_text = re.sub(r'\b\d+\b', '', clean_text)

    search_tokens = [t.strip() for t in re.split(r'[\s,]+', clean_text)
                     if len(t.strip()) >= 2 or "'" in t or '"' in t]

    if not search_tokens:
        return "ERROR_TOKENS|Could not extract item description."

    best_rows = []
    for idx, row in enumerate(data[1:], start=2):
        if len(row) < 3:
            continue
        full_row_name = f"{row[0]} {row[1]} {row[2]}".strip().lower()
        matches = sum(1 for token in search_tokens if token in full_row_name)
        if matches > 0:
            best_rows.append((matches, idx, f"{row[0]} {row[1]} {row[2]}'"))

    best_rows.sort(key=lambda x: x[0], reverse=True)

    if not best_rows:
        return f"NOT_FOUND|{search_tokens}"

    # Проверка на неоднозначность совпадений
    if len(best_rows) > 1 and best_rows[0][0] == best_rows[1][0]:
        alternatives = [r[2] for r in best_rows[:3]]
        return f"AMBIGUOUS|{', '.join(alternatives)}"

    target_row_idx = best_rows[0][1]
    matched_item_name = best_rows[0][2]
    
    target_col_idx = 4  # На листе LUMBER колонка Stock — это 4-й столбец (D)
    row_data = data[target_row_idx - 1]
    
    try:
        current_val_str = row_data[target_col_idx - 1].strip() if len(row_data) >= target_col_idx else "0"
        current_val_str = current_val_str.replace("-", "0").strip()
        current_val = int(current_val_str) if current_val_str.isdigit() else 0
    except Exception:
        current_val = 0

    new_val = current_val + qty_change
    if new_val < 0:
        new_val = 0

    success = update_cell_direct("LUMBER", target_row_idx, target_col_idx, new_val)
    if success:
        return f"SUCCESS|{matched_item_name}|{current_val}|{new_val}"
    else:
        return "ERROR_WRITE|Failed to write to Google Sheet cell."

def build_parts_context():
    data = get_sheet_data("PARTS")
    if not data:
        return "ERROR: Could not load data from main sheet."

    headers = []
    header_row_idx = 2
    for idx in [2, 1, 0]:
        if len(data) > idx:
            row_check = [cell.strip() for cell in data[idx] if cell.strip()]
            if any('x' in c.lower() or 'stock' in c.lower() for c in row_check):
                headers = [cell.strip() for cell in data[idx]]
                header_row_idx = idx
                break
                
    if not headers:
        headers = [cell.strip() for cell in data[2]] if len(data) > 2 else []

    table_text = "STRUCTURE OF THE TABLE:\n"
    table_text += "COLUMNS: " + " | ".join(headers) + "\n\n"
    table_text += "DATA:\n"
    
    start_row = header_row_idx + 1
    for row in data[start_row:]:
        if len(row) < 2: continue
        category = row[0].strip()
        sub_item = row[1].strip()
        if not category and not sub_item: continue
            
        full_name = f"{category} {sub_item}".strip()
        remaining_cells = [row[i].strip() if i < len(row) else "" for i in range(2, len(headers))]
        table_text += f"{full_name} | " + " | ".join(remaining_cells) + "\n"
        
    return table_text

SYSTEM_PROMPT = """You are CBG Manager — a precise AI assistant for Cedar-Built Greenhouses (Abbotsford, Canada).

HOW TO READ THE DATA:
1. The inventory data is provided below as a text table where columns are separated by '|'.
2. The headers of the columns (including greenhouse sizes like '10x18', '10x20') are defined in 'COLUMNS'.
3. Scan rows under 'DATA'. If a size column has a number > 0, include it. If it is empty, '-', or '0', ignore it.

STRICT RULES:
- Use ONLY the provided data. Never invent or hallucinate parts.
- Always respond in English.
"""

user_conversations = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")

    logger.info(f"Message from {user_name}: {message_text}")
    text_lower = message_text.lower()
    
    # Регулярка для перехвата действий со складом
    match_change = re.search(r'(took|added|minus|plus|\+|\-)\s*(\d+)', text_lower)
    
    if match_change:
        action = match_change.group(1)
        quantity = int(match_change.group(2))
        change_sign = -quantity if action in ['took', 'minus', '-'] else quantity
        
        result = update_lumber_stock(message_text, change_sign)
        
        # Защитная проверка: если функция вернула None или пустую строку
        if not result or not isinstance(result, str):
            logger.error(f"Incomplete execution in stock function. Result is: {result}")
            await update.message.reply_text("⚠️ Internal error while modifying database. Check script execution logic.")
            return

        if result.startswith("SUCCESS"):
            _, item_name, old_qty, new_qty = result.split("|")
            action_verb = "removed from" if change_sign < 0 else "added to"
            await update.message.reply_text(
                f"✅ **Stock Updated!**\n"
                f"Item: `{item_name}`\n"
                f"Action: Successfully {action_verb} stock by {quantity} pcs.\n"
                f"Previous Stock: {old_qty} → **Current Stock: {new_qty}**"
            )
            return
        elif result.startswith("AMBIGUOUS"):
            _, alternatives = result.split("|")
            await update.message.reply_text(
                f"⚠️ **Specify item size!** I found multiple matches:\n`{alternatives}`\n\n"
                f"Please include profile details (e.g., '2x4' or '2x6')."
            )
            return
        elif result.startswith("NOT_FOUND"):
            await update.message.reply_text(
                f"❌ **Item not found!** Could not match any item on the 'LUMBER' sheet.\n"
                f"Please verify specifications (Size, Grade, Length)."
            )
            return
        else:
            await update.message.reply_text(f"⚠️ Stock operation notification: {result}")
            return

    # Обычный диалог с Claude 4.5
    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({
        "role": "user",
        "content": f"[{user_name}, {current_time}]: {message_text}"
    })

    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    parts_data = build_parts_context()
    system_instruction = SYSTEM_PROMPT + f"\n\nINVENTORY DATA FROM GOOGLE SHEETS:\n{parts_data}"
    system_instruction += f"\n\nCurrent date/time inside the shop: {current_date}, {current_time}. Always respond in English."

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            system=system_instruction,
            messages=user_conversations[user_id]
        )
        reply = response.content[0].text
        
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply
        })
        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Claude error: {e}")
        await update.message.reply_text("Sorry, I'm having trouble processing this request right now.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: No TELEGRAM_BOT_TOKEN found!")
        return
        
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot is running on Claude 4.5...")
    app.run_polling()

if __name__ == "__main__":
    main()
