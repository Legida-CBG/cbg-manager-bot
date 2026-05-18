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

SPREADSHEET_ID = "1NNb7CeNl9gU5TXbJTGvx5RsMMvxgPY39J4nWPFSprBI"

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

def build_lumber_context():
    data = get_sheet_data("LUMBER")
    if not data:
        return "ERROR: Could not load LUMBER sheet."
    
    text = "LUMBER INVENTORY:\n"
    text += "Size | Category | Length | In Stock | Min Stock\n"
    text += "-" * 50 + "\n"
    
    for row in data[2:]:  # пропускаем 2 строки заголовков
        if len(row) < 4:
            continue
        size = row[0].strip()
        category = row[1].strip()
        length = row[2].strip()
        in_stock = row[3].strip()
        min_stock = row[4].strip() if len(row) > 4 else ""
        
        if not size and not category:
            continue
            
        text += f"{size} | {category} | {length} | {in_stock} | {min_stock}\n"
    
    return text

def build_parts_context():
    data = get_sheet_data("PARTS")
    if not data:
        return "ERROR: Could not load PARTS sheet."
    
    headers = [cell.strip() for cell in data[0]]
    
    text = "GREENHOUSE PARTS LIST:\n"
    text += " | ".join(headers) + "\n"
    text += "-" * 80 + "\n"
    
    for row in data[1:]:
        if len(row) < 2:
            continue
        item = row[0].strip()
        size_code = row[1].strip() if len(row) > 1 else ""
        if not item and not size_code:
            continue
        cells = [row[i].strip() if i < len(row) else "" for i in range(len(headers))]
        text += " | ".join(cells) + "\n"
    
    return text

SYSTEM_PROMPT = """You are CBG Manager — an AI assistant for Cedar-Built Greenhouses wood shop in Abbotsford, Canada.

You have access to two data sources:

1. LUMBER INVENTORY — current stock of lumber (2x4, 2x6, etc.)
2. GREENHOUSE PARTS LIST — parts needed for each greenhouse size

RULES:
- Always use the data provided. Never invent numbers.
- Answer in English.
- Be concise and clear.
- For lumber questions: look up the exact size, category and length.
- For parts questions: find the greenhouse size column and list all parts with quantities > 0.

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

    lumber_data = build_lumber_context()
    parts_data = build_parts_context()
    
    system_instruction = SYSTEM_PROMPT.format(
        lumber_context=lumber_data,
        parts_context=parts_data
    )
    system_instruction += f"\nCurrent date/time: {current_date}, {current_time}."

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
    logger.info("CBG Manager Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
