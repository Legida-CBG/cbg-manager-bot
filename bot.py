import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    """Update a cell using Google Sheets API directly"""
    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds)
        
        # Convert col number to letter (1=A, 2=B, 3=C, 4=D)
        col_letter = chr(64 + col_number)
        range_name = f"{sheet_name}!{col_letter}{row_number}"
        
        body = {"values": [[value]]}
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

def update_lumber_stock(lumber, category, length, change, operation):
    try:
        data = get_sheet_data("Lumber")
        if not data:
            return False, "Could not read lumber data"
        
        search_lumber = lumber.strip().lower()
        search_category = category.strip().lower()
        search_length = length.strip().lower().replace("'", "").replace('"', '').strip()
        
        for i, row in enumerate(data[1:], start=2):
            if len(row) < 4:
                continue
            
            row_lumber = row[0].strip().lower()
            row_category = row[1].strip().lower()
            row_length = row[2].strip().lower().replace("'", "").replace('"', '').strip()
            
            if row_lumber == search_lumber and row_category == search_category and row_length == search_length:
                try:
                    current = int(row[3].replace(',', '')) if row[3].strip() else 0
                except:
                    current = 0
                
                if operation == 'add':
                    new_value = current + change
                    action = f"Added {change} pcs"
                else:
                    if current < change:
                        return False, f"Cannot subtract {change} — only {current} in stock!"
                    new_value = current - change
                    action = f"Removed {change} pcs"
                
                success = update_cell_direct("LUMBER", i, 4, new_value)
                
                if success:
                    return True, f"{action}. {lumber} {category} @ {length}': {current} → {new_value} pcs"
                else:
                    return False, "Failed to update Google Sheets"
        
        return False, f"Item not found: {lumber} {category} @ {length}'"
        
    except Exception as e:
        logger.error(f"Update error: {e}")
        return False, f"Error: {e}"

def build_lumber_context():
    data = get_sheet_data("Lumber")
    if not data:
        return "ERROR: Could not load lumber data."
    
    table_text = "LUMBER INVENTORY:\n\n"
    for row in data[1:]:
        if not row or not row[0].strip():
            continue
        lumber = row[0].strip()
        category = row[1].strip() if len(row) > 1 else ""
        length = row[2].strip() if len(row) > 2 else ""
        in_stok = row[3].strip() if len(row) > 3 else "0"
        min_stock = row[4].strip() if len(row) > 4 else "0"
        
        low = ""
        try:
            if int(in_stok.replace(',','')) < int(min_stock.replace(',','')):
                low = " ⚠️ LOW"
            else:
                low = " ✅"
        except:
            pass
        
        table_text += f"{lumber} {category} @ {length}': IN_STOK={in_stok} | MIN_STOCK={min_stock}{low}\n"
    
    return table_text

def build_parts_context():
    data = get_sheet_data("Parts")
    if not data:
        return "ERROR: Could not load parts data."
    
    headers = []
    header_row_idx = 0
    for idx in [0, 1, 2]:
        if len(data) > idx:
            row_check = [cell.strip() for cell in data[idx] if cell.strip()]
            if any('x' in c.lower() for c in row_check):
                headers = [cell.strip() for cell in data[idx]]
                header_row_idx = idx
                break
    if not headers:
        headers = [cell.strip() for cell in data[0]]
        header_row_idx = 0
    
    table_text = "GREENHOUSE PARTS LIST:\n"
    table_text += "COLUMNS: " + " | ".join(headers) + "\n\n"
    table_text += "DATA:\n"
    
    for row in data[header_row_idx + 1:]:
        if len(row) < 2:
            continue
        category = row[0].strip()
        sub_item = row[1].strip()
        if not category and not sub_item:
            continue
        full_name = f"{category} {sub_item}".strip()
        remaining_cells = []
        for i in range(2, len(headers)):
            remaining_cells.append(row[i].strip() if i < len(row) else "")
        table_text += f"{full_name} | " + " | ".join(remaining_cells) + "\n"
    
    return table_text

SYSTEM_PROMPT = """You are CBG Manager — AI assistant for Cedar-Built Greenhouses in Abbotsford, Canada.

You manage lumber inventory and greenhouse parts information.

LUMBER INVENTORY RULES:
- IN_STOK = current quantity on hand
- MIN_STOCK = minimum threshold
- Format: [Lumber] [Category] @ [Length]': [IN_STOK] pcs (Min: [MIN_STOCK])

STOCK UPDATE DETECTION:
When staff mentions receiving or using lumber, extract:
1. Lumber size (e.g. 2x4, 2x6)
2. Category (e.g. STK, Clear, SPF)
3. Length (e.g. 6, 8)
4. Quantity (number)
5. Operation: ADD (received/got/delivered) or SUBTRACT (used/took/pulled)

When you detect a stock update, respond with EXACTLY this format on the FIRST line:
STOCK_UPDATE: [ADD or SUBTRACT] | [lumber] | [category] | [length] | [quantity]

Example: STOCK_UPDATE: SUBTRACT | 2x4 | STK | 6 | 100

Then on the next lines write a friendly confirmation message.

GREENHOUSE PARTS RULES:
- Find the requested greenhouse size in COLUMNS
- List only items with quantity > 0
- NEVER invent quantities
- If size not found, list available sizes

For work time tracking:
- "I'm here", "arrived" → confirm arrival with time
- "going home", "leaving" → confirm departure
- "lunch" → confirm lunch break

{lumber_context}

{parts_context}
"""

user_conversations = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")

    logger.info(f"Message from {user_name}: {message_text}")

    if user_id not in user_conversations:
        user_conversations[user_id] = []

    user_conversations[user_id].append({
        "role": "user",
        "content": f"[{user_name}, {current_time}]: {message_text}"
    })

    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    lumber_context = build_lumber_context()
    parts_context = build_parts_context()
    system_instruction = SYSTEM_PROMPT.format(
        lumber_context=lumber_context,
        parts_context=parts_context
    )
    system_instruction += f"\n\nCurrent date/time: {current_date}, {current_time}."

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=system_instruction,
            messages=user_conversations[user_id]
        )
        reply = response.content[0].text
        
        if reply.startswith("STOCK_UPDATE:"):
            lines = reply.split('\n', 1)
            update_line = lines[0]
            user_reply = lines[1].strip() if len(lines) > 1 else "Stock updated!"
            
            try:
                parts = update_line.replace("STOCK_UPDATE:", "").strip().split("|")
                operation = parts[0].strip().upper()
                lumber = parts[1].strip()
                category = parts[2].strip()
                length = parts[3].strip()
                quantity = int(parts[4].strip())
                
                op = 'add' if operation == 'ADD' else 'subtract'
                success, msg = update_lumber_stock(lumber, category, length, quantity, op)
                
                if success:
                    final_reply = f"✅ {msg}\n\n{user_reply}"
                else:
                    final_reply = f"❌ {msg}\n\n{user_reply}"
                    
                await update.message.reply_text(final_reply)
            except Exception as e:
                logger.error(f"Parse error: {e}")
                await update.message.reply_text(user_reply)
        else:
            await update.message.reply_text(reply)
        
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply
        })

    except Exception as e:
        logger.error(f"Claude error: {e}")
        await update.message.reply_text("Sorry, I'm having trouble right now. Please try again.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        return
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
