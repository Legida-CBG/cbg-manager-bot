import os
import json
import logging
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация ИИ-клиента Claude
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def get_sheet_connection():
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open("Cedar-Built")
        sheet = spreadsheet.worksheet("Лист1")
        return sheet
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheets: {e}")
        return None

def get_parts_data():
    sheet = get_sheet_connection()
    if sheet:
        return sheet.get_all_values()
    return None

def update_inventory_stock(text_message, qty_change):
    """
    Сканирует таблицу, находит лучшую строку по совпадению ключевых слов и обновляет Lumber Stock
    """
    sheet = get_sheet_connection()
    if not sheet:
        return "ERROR: Database connection failed."
        
    data = sheet.get_all_values()
    if len(data) < 3:
        return "ERROR: Spreadsheet is too small."
        
    headers = [cell.strip().lower() for cell in data[2]]
    
    # Ищем индекс колонки Lumber Stock
    target_col_idx = -1
    for i, h in enumerate(headers):
        if 'lumber stock' in h or 'stock' in h:
            target_col_idx = i
            break
            
    if target_col_idx == -1:
        return "ERROR: 'Lumber Stock' column not found in headers."
        
    # Очищаем текст сообщения от цифр и команд для точного поиска деталей
    words_to_remove = ['took', 'added', 'minus', 'plus', 'of', 'from', 'warehouse', 'stock', 'to', 'the', 'items', 'pieces', 'pcs']
    clean_text = text_message.lower()
    for w in words_to_remove:
        clean_text = clean_text.replace(w, '')
    clean_text = re.sub(r'\d+', '', clean_text) # Удаляем сами числа количества
    
    # Извлекаем все значимые токены (например: ['2x4', 'stk', "6'"])
    search_tokens = [t.strip() for t in re.split(r'[\s,]+', clean_text) if len(t.strip()) >= 2 or "'" in t or '"' in t]
    
    if not search_tokens:
        return "ERROR: Could not extract item description from message."
        
    target_row_idx = -1
    max_matches = 0
    
    # Ищем строку с максимальным совпадением токенов
    for idx, row in enumerate(data[3:], start=4):
        if len(row) < 2: continue
        full_row_name = f"{row[0]} {row[1]}".strip().lower()
        
        matches = sum(1 for token in search_tokens if token in full_row_name)
        if matches > max_matches and matches >= len(search_tokens) - 1:
            max_matches = matches
            target_row_idx = idx

    if target_row_idx == -1:
        return f"ERROR: No matching lumber item found for tokens: {search_tokens}"
        
    # Читаем текущее значение ячейки количества
    try:
        current_val_str = data[target_row_idx - 1][target_col_idx].strip()
        current_val_str = current_val_str.replace("-", "0").strip()
        current_val = int(current_val_str) if current_val_str.isdigit() else 0
    except Exception:
        current_val = 0
        
    new_val = current_val + qty_change
    if new_val < 0:
        new_val = 0  # Запас на складе не уходит в минус
        
    # Записываем обновленное число в Google Таблицу
    try:
        sheet.update_cell(target_row_idx, target_col_idx + 1, str(new_val))
        matched_item_name = f"{data[target_row_idx - 1][0]} {data[target_row_idx - 1][1]}".strip()
        return f"SUCCESS|{matched_item_name}|{current_val}|{new_val}"
    except Exception as e:
        return f"ERROR: Failed to update cell: {e}"

def build_parts_context():
    data = get_parts_data()
    if not data:
        return "ERROR: Could not load data from Google Sheets."

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
        
        remaining_cells = []
        for i in range(2, len(headers)):
            if i < len(row):
                remaining_cells.append(row[i].strip())
            else:
                remaining_cells.append("")
                
        table_text += f"{full_name} | " + " | ".join(remaining_cells) + "\n"
        
    return table_text

SYSTEM_PROMPT = """You are CBG Manager — a precise AI assistant for Cedar-Built Greenhouses.

HOW TO READ THE DATA:
1. Inventory data has columns separated by '|'. Greenhouse sizes and 'Lumber Stock' are defined in 'COLUMNS'.
2. When answering user questions, use the exact quantities matching the columns.

STRICT RULES:
- Never guess or hallucinate parts. 
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
    
    # Регулярное выражение для поиска количества и действия (поддерживает "+ 10", "- 5", "took 10", "added 5")
    match_change = re.search(r'(took|added|minus|plus|\+|\-)\s*(\d+)', text_lower)
    
    if match_change:
        action = match_change.group(1)
        quantity = int(match_change.group(2))
        
        # Определяем направление изменения
        change_sign = -quantity if action in ['took', 'minus', '-'] else quantity
        
        # Пытаемся обновить ячейку склада
        result = update_inventory_stock(message_text, change_sign)
        
        if result.startswith("SUCCESS"):
            _, item_name, old_qty, new_qty = result.split("|")
            action_verb = "removed" if change_sign < 0 else "added"
            await update.message.reply_text(
                f"✅ **Stock Updated!**\n"
                f"Item: `{item_name}`\n"
                f"Action: Successfully {action_verb} {quantity} pcs.\n"
                f"Previous Stock: {old_qty} → **Current Stock: {new_qty}**"
            )
            return
        else:
            logger.warning(f"Stock script bypassed to AI: {result}")

    # Стандартная обработка через Claude
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
        await update.message.reply_text("Sorry, I'm having trouble processing this request right now.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: No TELEGRAM_BOT_TOKEN found!")
        return
        
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot with Auto Stock Search starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
