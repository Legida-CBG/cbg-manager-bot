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

# Используем актуальное название модели
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def get_parts_data():
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open("Cedar-Built")
        sheet = spreadsheet.worksheet("Лист1")
        # Получаем все данные как список списков
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

    # Мы просто переводим таблицу в текстовый вид (CSV-like), 
    # чтобы Claude сам нашел нужную колонку. Это надежнее.
    table_text = ""
    for row in data:
        # Убираем лишние пробелы и объединяем через табуляцию или |
        clean_row = [cell.strip() if cell else "" for cell in row]
        table_text += " | ".join(clean_row) + "\n"
    
    return table_text

SYSTEM_PROMPT = """You are CBG Manager — a precise AI assistant for Cedar-Built Greenhouses.

CRITICAL RULES:
1. Use ONLY the "RAW INVENTORY DATA" provided below to answer questions about parts and quantities.
2. If a user asks for a size (e.g., "10x18"), find the column header that exactly matches or contains that size.
3. List only items where the quantity for that specific size is greater than 0 or not empty.
4. If you cannot find the requested size in the headers, list the available sizes found in the headers and ask for clarification.
5. DO NOT invent or assume any parts or codes. If it's not in the table, it doesn't exist.
6. Always format the output as a clean list: Item Name - Code: Quantity.

RAW INVENTORY DATA:
{parts_context}

For work time tracking:
- Arrived/At work -> confirm arrival.
- Leaving/Done -> confirm departure.
- Lunch -> confirm lunch break.
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

    # Ограничиваем историю, чтобы не перегружать память
    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    # Обновляем контекст из таблицы при каждом запросе
    parts_data = build_parts_context()
    system_instruction = SYSTEM_PROMPT.format(parts_context=parts_data)
    system_instruction += f"\n\nCurrent date/time: {current_date}, {current_time}. Respond in English."

    try:
        response = claude.messages.create(
            model="claude-3-5-sonnet-latest", # Исправленная модель
            max_tokens=1024,
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
        await update.message.reply_text("I'm having trouble accessing my brain right now. Try again in a second!")

def main():
    # Убедись, что эти переменные добавлены в Railway
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN found!")
        return
        
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
