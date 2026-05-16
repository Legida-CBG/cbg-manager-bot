import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def get_parts_data():
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open("Cedar-Built")
        sheet = spreadsheet.worksheet("Лист1")
        data = sheet.get_all_values()
        logger.info(f"SUCCESS: Loaded {len(data)} rows from Google Sheets")
        return data
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return None

def build_parts_context():
    data = get_parts_data()
    if not data:
        return "ERROR: Could not load data from Google Sheets."

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

    logger.info(f"Headers found at row {header_row_idx}: {headers}")

    table_text = "STRUCTURE OF THE TABLE:\n"
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
            if i < len(row):
                remaining_cells.append(row[i].strip())
            else:
                remaining_cells.append("")
        table_text += f"{full_name} | " + " | ".join(remaining_cells) + "\n"

    return table_text

SYSTEM_PROMPT = """You are CBG Manager — a precise AI assistant for Cedar-Built Greenhouses (a woodworking shop in Abbotsford, Canada).

HOW TO READ THE DATA:
1. The inventory data is provided below as a text table where columns are separated by '|'.
2. The headers of the columns (including greenhouse sizes like '10x18', '10x20', '10x24EX') are defined in the 'COLUMNS' section.
3. When a user asks for a specific greenhouse size (e.g., "10x18"), find that exact column in COLUMNS.
4. For each row in DATA, check the value in that size column.
5. If the value is a number greater than 0, include this part in the list.
6. If the cell is empty or 0, skip it completely.

STRICT RULES:
- Use ONLY the provided data. NEVER invent parts, codes, or quantities.
- Format output as a clean list: Item Name: Quantity.
- If the requested size is not found, list all available sizes from COLUMNS and ask to clarify.

RAW INVENTORY DATA:
{parts_context}

For work time tracking:
- "I'm here", "arrived", "at work" → confirm arrival and note the time.
- "going home", "leaving", "done for the day" → confirm departure.
- "lunch", "lunch break" → confirm lunch break start.
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

    parts_data = build_parts_context()
    system_instruction = SYSTEM_PROMPT.format(parts_context=parts_data)
    system_instruction += f"\n\nCurrent date/time: {current_date}, {current_time}. Always respond in English."

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
