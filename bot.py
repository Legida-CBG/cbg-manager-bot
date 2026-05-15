import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация ИИ-клиента Claude с актуальной и стабильной моделью
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def get_parts_data():
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
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
    header_row_idx = 2  # По умолчанию берем 3-ю строку (индекс 2)
    
    # Умный поиск: проверяем строки 3, 2 и 1, чтобы найти ту, где есть размеры с буквой 'x'
    for idx in [2, 1, 0]:
        if len(data) > idx:
            row_check = [cell.strip() for cell in data[idx] if cell.strip()]
            if any('x' in c.lower() for c in row_check):
                headers = [cell.strip() for cell in data[idx]]
                header_row_idx = idx
                break
                
    # Если автопоиск вдруг не сработал, жестко берем 3-ю строку
    if not headers:
        headers = [cell.strip() for cell in data[2]] if len(data) > 2 else []

    # Выводим в логи Railway, что именно прочитал бот в шапке таблицы
    logger.info(f"DEBUG HEADERS FOUND: {headers}")
    
    table_text = "STRUCTURE OF THE TABLE:\n"
    table_text += "COLUMNS: " + " | ".join(headers) + "\n\n"
    table_text += "DATA:\n"
    
    # Данные начинаются строго со следующей строки после найденных заголовков
    start_row = header_row_idx + 1
    for row in data[start_row:]:
        if len(row) < 2:
            continue
            
        category = row[0].strip()
        sub_item = row[1].strip()
        
        # Если вся строка пустая, пропускаем её
        if not category and not sub_item:
            continue
            
        # Склеиваем колонку А и Б для получения уникального и точного названия детали
        full_name = f"{category} {sub_item}".strip()
        
        # Передаем ячейки количества, строго выравнивая их по длине заголовков шапки
        remaining_cells = []
        for i in range(2, len(headers)):
            if i < len(row):
                remaining_cells.append(row[i].strip())
            else:
                remaining_cells.append("")
                
        # Записываем строку в формате понятного текстового CSV
        table_text += f"{full_name} | " + " | ".join(remaining_cells) + "\n"
        
    return table_text

SYSTEM_PROMPT = """You are CBG Manager — a precise AI assistant for Cedar-Built Greenhouses (a woodworking shop in Abbotsford, Canada).

HOW TO READ THE DATA:
1. The inventory data is provided below as a text table where columns are separated by '|'.
2. The headers of the columns (including greenhouse sizes like '10x18', '10x20', '10x24EX') are defined in the 'COLUMNS' section.
3. When a user asks for a specific greenhouse size (e.g., "10x18"), look at the 'COLUMNS' row to find the exact position (index) of that size column.
4. Scan the rows under 'DATA'. For each row, check the value in that specific size column.
5. If the value is a number greater than 0, it means this part is required. Include it in the list.
6. If the cell is empty, contains '-', '0', or spaces, this part is NOT needed for this size. Completely ignore it.

STRICT RULES:
- Use ONLY the provided data below. Never invent, hallucinate, or guess parts, codes, or quantities.
- Always combine the item names accurately as provided (e.g., if row starts with 'BASEWALL GW35', the item name is 'BASEWALL GW35').
- Format the final output as a clean, structured list grouped by logical categories if possible (e.g., Basewall, Roof Vents, etc.), showing: Item Name: Quantity.
- If the requested size is not found in the columns, gently list all available sizes from the headers and ask the user to clarify.

RAW INVENTORY DATA:
{parts_context}

For work time tracking:
- When staff says "I'm here", "arrived", "at work" → confirm arrival and note the time.
- When staff says "going home", "leaving", "done for the day" → confirm departure.
- When staff says "lunch", "lunch break" → confirm lunch break start.
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

    # Ограничиваем историю диалога (последние 10 сообщений) для экономии контекста
    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    # Динамически подгружаем свежую матрицу данных из таблицы при каждом запросе
    parts_data = build_parts_context()
    system_instruction = SYSTEM_PROMPT.format(parts_context=parts_data)
    system_instruction += f"\n\nCurrent date/time inside the shop: {current_date}, {current_time}. Always respond in English."

    try:
        response = claude.messages.create(
            model="claude-3-5-sonnet-latest", 
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
        await update.message.reply_text("Sorry, I'm having trouble processing this request right now. Please try again.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: No TELEGRAM_BOT_TOKEN found in environment variables!")
        return
        
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
