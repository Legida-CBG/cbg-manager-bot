import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def get_sheet_data(sheet_name):
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open("Cedar-Built")
        sheet = spreadsheet.worksheet(sheet_name)
        data = sheet.get_all_values()
        logger.info(f"SUCCESS: Loaded {len(data)} rows from sheet '{sheet_name}'")
        return data
    except Exception as e:
        logger.error(f"Sheets error ({sheet_name}): {e}")
        return None

def build_lumber_context():
    data = get_sheet_data("Lumber")
    if not data:
        return "ERROR: Could not load lumber data."
    
    headers = [cell.strip() for cell in data[0]]
    logger.info(f"Lumber headers: {headers}")
    
    table_text = "LUMBER INVENTORY:\n"
    table_text += "COLUMNS: " + " | ".join(headers) + "\n"
    table_text += "NOTE: Column D = 'In Stock' = CURRENT quantity. Column E = 'Min Stock' = minimum threshold.\n\n"
    table_text += "DATA (Lumber | Category | Length | In Stock | Min Stock | Min Order):\n"
    
    for row in data[1:]:
        if not row or not row[0].strip():
            continue
        clean_row = [cell.strip() for cell in row]
        table_text += " | ".join(clean_row) + "\n"
    
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
    
    logger.info(f"Parts headers at row {header_row_idx}: {headers}")
    
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

You have access to TWO data sources:
1. LUMBER INVENTORY — current stock of lumber materials
2. GREENHOUSE PARTS LIST — parts needed for each greenhouse size

HOW TO USE LUMBER DATA:
- The data has these columns: Lumber | Category | Length | In Stock | Min Stock | Min Order
- "In Stock" is the CURRENT quantity on hand — use this number
- "Min Stock" is the minimum threshold — use this only for comparison
- Show each item separately with its length
- Format: [Lumber] [Category] @ [Length]: [In Stock value] pcs (Min Stock: [Min Stock value])
- Example: 2x4 STK @ 4': 1,162 pcs (Min Stock: 300)
- Add ⚠️ LOW STOCK if In Stock is below Min Stock
- Add ✅ if In Stock is above Min Stock
- CRITICAL: Never mix up "In Stock" and "Min Stock" columns

HOW TO USE PARTS DATA:
- COLUMNS row shows greenhouse sizes (10x18, 12x12, etc.)
- Find the requested size column
- List only items where quantity > 0
- Format: Item Name (Code): Quantity
- NEVER invent quantities — use only what is in the data
- If size not found, list all available sizes

STRICT RULES:
- Use ONLY provided data. Never invent anything.
- Always respond in English.

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
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply
        })
        await update.message.reply_text(reply)

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
