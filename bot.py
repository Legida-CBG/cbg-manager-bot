import os
import json
import logging
import base64
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

# Хранилище ожидающих подтверждений: user_id → данные для записи
pending_confirmations = {}

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

def get_worksheet(sheet_name):
    creds = get_credentials()
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(sheet_name)

def build_lumber_context():
    data = get_sheet_data("LUMBER")
    if not data:
        return "ERROR: Could not load LUMBER sheet."
    
    text = "LUMBER INVENTORY:\n"
    text += "Size | Category | Length | In Stock | Min Stock\n"
    text += "-" * 50 + "\n"
    
    for row in data[2:]:
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

def extract_pdf_data_with_claude(pdf_bytes):
    """Отправляет PDF в Claude и получает структурированный список деталей."""
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_base64
                    }
                },
                {
                    "type": "text",
                    "text": """Look at PAGE 1 ONLY of this PDF. It contains a parts specification table for a greenhouse order.

Extract the following:
1. Greenhouse model - found at the top of page 1. 
   Rules for model name:
   - Keep only the size (e.g. 12x20) and EX or EX2 suffix if present
   - Remove any other words like SHD, SHED, EXTENSION, or customer names
   - No spaces: write as 12x20EX not "12x20 EX"
   - Examples: "12x20 EX SHD" → "12x20EX", "8x12" → "8x12", "10x16 EX2" → "10x16EX2"

2. All parts from the table with their ITEM, SIZE/CODE, and QUANT. (quantity)

Return ONLY valid JSON in this exact format, no other text:
{
  "model": "12x20EX",
  "parts": [
    {"item": "BASEWALL", "size_code": "5'-7 1/2\"", "quantity": 2},
    {"item": "ROOF VENTS", "size_code": "RV-3", "quantity": 2}
  ]
}

Important: use only the QUANT. column for quantity (ignore CRATE # column)."""
                }
            ]
        }]
    )
    
    raw = response.content[0].text.strip()
    # Убираем markdown обёртку если есть
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    
    return json.loads(raw)

def write_to_list_sheet(model, parts):
    """
    Записывает данные в лист LIST.
    Логика совпадения (по приоритету):
      1. Item + Size/Code совпадают точно
      2. Только Size/Code совпадает точно (название могло отличаться)
      3. Ничего не найдено → добавить новую строку
    """
    sheet = get_worksheet("LIST")
    data = sheet.get_all_values()

    if not data:
        raise Exception("LIST sheet is empty")

    headers = data[0]

    # Ищем колонку с моделью теплицы
    model_col_index = None
    for i, h in enumerate(headers):
        if h.strip() == model.strip():
            model_col_index = i
            break

    column_existed = model_col_index is not None

    # Если колонки нет — создаём
    if model_col_index is None:
        model_col_index = len(headers)
        sheet.update_cell(1, model_col_index + 1, model)
        headers.append(model)

    # Строим два индекса:
    # 1. (item_upper, size_code_upper) → row_num  — точное совпадение обоих
    # 2. size_code_upper → row_num                 — совпадение только по коду
    index_both = {}
    index_code = {}

    for row_num, row in enumerate(data[1:], start=2):
        item = row[0].strip() if len(row) > 0 else ""
        size_code = row[1].strip() if len(row) > 1 else ""
        if not item and not size_code:
            continue
        key_both = (item.upper(), size_code.upper())
        index_both[key_both] = row_num
        if size_code:
            # Если один код встречается в нескольких строках — берём первое вхождение
            if size_code.upper() not in index_code:
                index_code[size_code.upper()] = row_num

    updated = 0
    added = 0
    matched_by_code = 0

    for part in parts:
        item = part["item"].strip()
        size_code = part["size_code"].strip()
        quantity = part["quantity"]

        key_both = (item.upper(), size_code.upper())

        if key_both in index_both:
            # Приоритет 1: точное совпадение Item + Size/Code
            row_num = index_both[key_both]
            sheet.update_cell(row_num, model_col_index + 1, quantity)
            updated += 1

        elif size_code.upper() in index_code:
            # Приоритет 2: совпадение только по Size/Code
            row_num = index_code[size_code.upper()]
            sheet.update_cell(row_num, model_col_index + 1, quantity)
            matched_by_code += 1

        else:
            # Приоритет 3: ничего не найдено — добавляем новую строку
            new_row = [""] * (model_col_index + 1)
            new_row[0] = item
            new_row[1] = size_code
            new_row[model_col_index] = quantity
            sheet.append_row(new_row)
            added += 1

    return {
        "updated": updated,
        "matched_by_code": matched_by_code,
        "added": added,
        "column_existed": column_existed
    }

def check_column_has_data(model):
    """Проверяет: есть ли уже данные в колонке с этой моделью теплицы."""
    data = get_sheet_data("LIST")
    if not data:
        return False
    
    headers = data[0]
    model_col_index = None
    for i, h in enumerate(headers):
        if h.strip() == model.strip():
            model_col_index = i
            break
    
    if model_col_index is None:
        return False  # Колонки вообще нет — данных нет
    
    # Проверяем есть ли хоть одна непустая ячейка в этой колонке
    for row in data[1:]:
        if model_col_index < len(row) and row[model_col_index].strip():
            return True
    
    return False

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

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает PDF файл отправленный в Telegram."""
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    
    await update.message.reply_text("📄 PDF received. Reading the order specification...")
    
    try:
        # Скачиваем PDF
        file = await update.message.document.get_file()
        pdf_bytes = await file.download_as_bytearray()
        
        await update.message.reply_text("🔍 Extracting parts list from page 1...")
        
        # Извлекаем данные через Claude
        extracted = extract_pdf_data_with_claude(bytes(pdf_bytes))
        model = extracted["model"]
        parts = extracted["parts"]
        
        logger.info(f"Extracted model: {model}, parts count: {len(parts)}")
        
        # Проверяем есть ли уже данные в колонке
        has_data = check_column_has_data(model)
        
        if has_data:
            # Сохраняем данные в ожидании подтверждения
            pending_confirmations[user_id] = {
                "model": model,
                "parts": parts
            }
            
            await update.message.reply_text(
                f"⚠️ Column *{model}* already has data in the LIST sheet.\n\n"
                f"Found *{len(parts)} parts* in this PDF.\n\n"
                f"Do you want to *overwrite* the existing data?\n\n"
                f"Reply *YES* to overwrite, or *NO* to cancel.",
                parse_mode="Markdown"
            )
        else:
            # Данных нет — пишем сразу
            await update.message.reply_text(f"✅ Found model *{model}* with *{len(parts)} parts*. Writing to LIST sheet...", parse_mode="Markdown")
            
            result = write_to_list_sheet(model, parts)

            await update.message.reply_text(
                f"✅ *Done!* Data written to LIST sheet.\n\n"
                f"📋 Model: *{model}*\n"
                f"🔄 Matched by name+code: *{result['updated']}*\n"
                f"🔍 Matched by code only: *{result['matched_by_code']}*\n"
                f"➕ New rows added: *{result['added']}*",
                parse_mode="Markdown"
            )
    
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error from Claude: {e}")
        await update.message.reply_text("❌ Could not read the PDF structure. Please make sure it's a standard CBG order specification.")
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await update.message.reply_text(f"❌ Error processing PDF: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text.strip()
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")

    logger.info(f"Message from {user_name}: {message_text}")

    # Проверяем ожидает ли этот пользователь подтверждения перезаписи
    if user_id in pending_confirmations:
        if message_text.upper() in ["YES", "Y", "ДА"]:
            data = pending_confirmations.pop(user_id)
            model = data["model"]
            parts = data["parts"]
            
            await update.message.reply_text(f"✍️ Overwriting data for *{model}*...", parse_mode="Markdown")
            
            try:
                result = write_to_list_sheet(model, parts)
                await update.message.reply_text(
                    f"✅ *Done!* Data overwritten in LIST sheet.\n\n"
                    f"📋 Model: *{model}*\n"
                    f"🔄 Updated rows: *{result['updated']}*\n"
                    f"➕ New rows added: *{result['added']}*",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Write error: {e}")
                await update.message.reply_text(f"❌ Error writing to sheet: {str(e)}")
            return
        
        elif message_text.upper() in ["NO", "N", "НЕТ"]:
            pending_confirmations.pop(user_id)
            await update.message.reply_text("❌ Cancelled. No data was changed.")
            return

    # Обычный разговор с Claude
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
    
    # Обработчик PDF файлов
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    
    # Обработчик текстовых сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("CBG Manager Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
