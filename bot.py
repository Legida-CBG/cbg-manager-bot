import os
import io
import re
import json
import logging
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from generate_pdf import generate_checks_pdf

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
1. Order number - found at the top (e.g. "2699")
2. Client name - found at the top (e.g. "Spicer")
3. Greenhouse model - found at the top of page 1.
   Rules for model name:
   - Keep ONLY the size (e.g. 12x20) and EX or EX2 suffix if present
   - Remove ALL other words: SHD, DRM, SHED, EXTENSION, customer names, or any other text
   - No spaces: write as 12x20EX not "12x20 EX"
   - Examples: "12x20 EX SHD" → "12x20EX", "10x14 DRM" → "10x14", "8x12" → "8x12"
4. All parts from the table with their ITEM, SIZE/CODE, and QUANT. (quantity)

Return ONLY valid JSON in this exact format, no other text:
{
  "order_num": "2699",
  "client_name": "Spicer",
  "model": "12x20EX",
  "parts": [
    {"item": "BASEWALL", "size_code": "5'-7 1/2\\"", "quantity": 2},
    {"item": "ROOF VENTS", "size_code": "RV-3", "quantity": 2}
  ]
}

Important: use only the QUANT. column for quantity (ignore CRATE # column)."""
                }
            ]
        }]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    return json.loads(raw)

def get_list_rows_for_model(model: str) -> list:
    """
    Читает лист LIST и возвращает список деталей для указанной модели.
    Только строки где количество > 0.
    """
    data = get_sheet_data("LIST")
    if not data:
        return []

    headers = data[0]

    # Ищем колонку модели
    model_col_index = None
    for i, h in enumerate(headers):
        if h.strip() == model.strip():
            model_col_index = i
            break

    if model_col_index is None:
        logger.warning(f"Model column '{model}' not found in LIST sheet")
        return []

    rows = []
    for row in data[1:]:
        item = row[0].strip() if len(row) > 0 else ""
        size_code = row[1].strip() if len(row) > 1 else ""
        quant_raw = row[model_col_index].strip() if model_col_index < len(row) else ""

        if not quant_raw:
            continue
        try:
            quant = int(quant_raw)
        except ValueError:
            try:
                quant = float(quant_raw)
            except ValueError:
                continue

        if quant > 0:
            rows.append({
                "item": item,
                "size_code": size_code,
                "quant": quant_raw,
            })

    logger.info(f"Found {len(rows)} parts for model '{model}' in LIST")
    return rows

def write_to_list_sheet(model, parts):
    sheet = get_worksheet("LIST")
    data = sheet.get_all_values()
    if not data:
        raise Exception("LIST sheet is empty")
    headers = data[0]
    model_col_index = None
    for i, h in enumerate(headers):
        if h.strip() == model.strip():
            model_col_index = i
            break
    column_existed = model_col_index is not None
    if model_col_index is None:
        model_col_index = len(headers)
        sheet.update_cell(1, model_col_index + 1, model)
        headers.append(model)
    index_both = {}
    index_code = {}

    def norm_apostrophe(s):
        return s.replace(chr(0x2019), chr(39)).replace(chr(0x2018), chr(39)).replace(chr(0x201c), chr(34)).replace(chr(0x201d), chr(34))

    for row_num, row in enumerate(data[1:], start=2):
        item = row[0].strip() if len(row) > 0 else ""
        size_code_raw = norm_apostrophe(row[1].strip()) if len(row) > 1 else ""
        size_code = size_code_raw.replace(' ', '')
        if not item and not size_code:
            continue
        key_both = (item.upper(), size_code.upper())
        index_both[key_both] = row_num
        if size_code:
            if size_code.upper() not in index_code:
                index_code[size_code.upper()] = row_num

    def normalize_code(code):
        code = code.replace(chr(0x2019), chr(39)).replace(chr(0x2018), chr(39))
        code = code.replace(chr(0x201c), chr(34)).replace(chr(0x201d), chr(34))
        result = list(code)
        for i, ch in enumerate(result):
            prev = result[i-1] if i > 0 else chr(0)
            nxt = result[i+1] if i < len(result)-1 else chr(0)
            if ch == 'S' and (prev.isdigit() or prev == '-') and (nxt.isdigit() or nxt == '-'):
                result[i] = '5'
            elif ch == 'O' and (prev.isdigit() or prev == '-') and (nxt.isdigit() or nxt == '-'):
                result[i] = '0'
            elif ch == '8' and (prev.isalpha() or prev == ' ' or prev == chr(0)) and (nxt.isalpha() or nxt == ' ' or nxt == '-' or nxt == chr(0)):
                result[i] = 'B'
        return ''.join(result).replace(' ', '')

    updated = 0
    skipped = 0
    matched_by_code = 0
    skipped_list = []

    for part in parts:
        item = part["item"].strip()
        size_code_raw = part["size_code"].strip()
        quantity = part["quantity"]
        size_code = size_code_raw.replace(" ", "")
        size_code_norm = normalize_code(size_code_raw)
        key_both = (item.upper(), size_code.upper())
        key_both_norm = (item.upper(), size_code_norm.upper())
        if key_both in index_both:
            sheet.update_cell(index_both[key_both], model_col_index + 1, quantity)
            updated += 1
        elif key_both_norm in index_both:
            sheet.update_cell(index_both[key_both_norm], model_col_index + 1, quantity)
            updated += 1
        elif size_code.upper() in index_code:
            sheet.update_cell(index_code[size_code.upper()], model_col_index + 1, quantity)
            matched_by_code += 1
        elif size_code_norm.upper() in index_code:
            sheet.update_cell(index_code[size_code_norm.upper()], model_col_index + 1, quantity)
            matched_by_code += 1
        else:
            skipped += 1
            skipped_list.append(f"{item} | {size_code_raw}")
            logger.info(f"SKIPPED (not found): {item} | {size_code_raw}")

    return {
        "updated": updated,
        "matched_by_code": matched_by_code,
        "skipped": skipped,
        "skipped_list": skipped_list,
        "column_existed": column_existed
    }

def check_column_has_data(model):
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
        return False
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

async def send_checks_pdf(update: Update, order_num: str, client_name: str, model: str):
    """Генерирует и отправляет Checks Sheet PDF в чат."""
    rows = get_list_rows_for_model(model)
    if not rows:
        await update.message.reply_text(
            f"⚠️ Could not generate Checks Sheet — model *{model}* not found in LIST sheet.",
            parse_mode="Markdown"
        )
        return

    pdf_bytes = generate_checks_pdf(
        order_num=order_num,
        client_name=client_name,
        model=model,
        rows=rows
    )

    filename = f"{order_num}_{client_name}_{model}_Checks.pdf"
    await update.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=filename,
        caption=f"📋 *Checks Sheet* — {order_num} {client_name} | {model}\n_{len(rows)} parts_",
        parse_mode="Markdown"
    )
    logger.info(f"Checks Sheet PDF sent: {filename} ({len(rows)} parts)")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает PDF файл отправленный в Telegram."""
    user_id = update.effective_user.id

    await update.message.reply_text("📄 PDF received. Reading the order specification...")

    try:
        file = await update.message.document.get_file()
        pdf_bytes = await file.download_as_bytearray()

        await update.message.reply_text("🔍 Extracting parts list from page 1...")

        extracted = extract_pdf_data_with_claude(bytes(pdf_bytes))
        model = extracted["model"]
        parts = extracted["parts"]
        order_num = extracted.get("order_num", "N/A")
        client_name = extracted.get("client_name", "Unknown")

        logger.info(f"Extracted: order={order_num}, client={client_name}, model={model}, parts={len(parts)}")

        has_data = check_column_has_data(model)

        if has_data:
            pending_confirmations[user_id] = {
                "model": model,
                "parts": parts,
                "order_num": order_num,
                "client_name": client_name,
            }
            await update.message.reply_text(
                f"⚠️ Column *{model}* already has data in the LIST sheet.\n\n"
                f"Found *{len(parts)} parts* in this PDF.\n\n"
                f"Do you want to *overwrite* the existing data?\n\n"
                f"Reply *YES* to overwrite, or *NO* to cancel.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"✅ Found order *{order_num}* — {client_name} | Model: *{model}* | *{len(parts)} parts*\nWriting to LIST sheet...",
                parse_mode="Markdown"
            )
            result = write_to_list_sheet(model, parts)

            skipped_text = ""
            if result['skipped_list']:
                skipped_text = "\n\n⚠️ *Not found:*\n" + "\n".join(f"• {s}" for s in result['skipped_list'])

            await update.message.reply_text(
                f"✅ *Done!* Data written to LIST sheet.\n\n"
                f"📋 Model: *{model}*\n"
                f"🔄 Matched by name+code: *{result['updated']}*\n"
                f"🔍 Matched by code only: *{result['matched_by_code']}*\n"
                f"⏭ Not found (skipped): *{result['skipped']}*"
                + skipped_text,
                parse_mode="Markdown"
            )

            # ✅ Генерируем и отправляем Checks Sheet PDF
            await update.message.reply_text("📄 Generating Checks Sheet PDF...")
            await send_checks_pdf(update, order_num, client_name, model)

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error from Claude: {e}")
        await update.message.reply_text("❌ Could not read the PDF structure. Please make sure it's a standard CBG order specification.")
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await update.message.reply_text(f"❌ Error processing PDF: {str(e)}")

def parse_new_order_message(text: str):
    """
    Парсит сообщение от Make:
    NEW_ORDER|2699-Spicer 12x20EX SHD
    Возвращает {"order_num", "client_name", "model"} или None.
    """
    text = text.strip()
    if not text.startswith("NEW_ORDER|"):
        return None
    payload = text[len("NEW_ORDER|"):].strip()
    parts = payload.split(" ", 1)
    if not parts or not parts[0]:
        return None
    order_client = parts[0]
    dash_idx = order_client.find("-")
    if dash_idx == -1:
        return None
    order_num = order_client[:dash_idx]
    client_name = order_client[dash_idx+1:]
    remainder = parts[1] if len(parts) > 1 else ""
    model_match = re.search(r'\d+[x×X]\d+(?:EX2?)?', remainder, re.IGNORECASE)
    if not model_match:
        return None
    model_raw = model_match.group(0)
    model = re.sub(r'[×X]', 'x', model_raw)
    model = re.sub(r'ex2?', lambda m: m.group(0).upper(), model, flags=re.IGNORECASE)
    return {"order_num": order_num, "client_name": client_name, "model": model}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text.strip()
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")

    logger.info(f"Message from {user_name}: {message_text}")

    # ✅ Обработка сообщения от Make
    order_data = parse_new_order_message(message_text)
    if order_data:
        order_num = order_data["order_num"]
        client_name = order_data["client_name"]
        model = order_data["model"]
        logger.info(f"NEW_ORDER received: {order_num} | {client_name} | {model}")
        await update.message.reply_text(
            f"📋 New order received!\n"
            f"*Order:* {order_num}\n"
            f"*Client:* {client_name}\n"
            f"*Model:* {model}\n\n"
            f"Generating Checks Sheet PDF...",
            parse_mode="Markdown"
        )
        await send_checks_pdf(update, order_num, client_name, model)
        return

    if user_id in pending_confirmations:
        if message_text.upper() in ["YES", "Y", "ДА"]:
            data = pending_confirmations.pop(user_id)
            model = data["model"]
            parts = data["parts"]
            order_num = data["order_num"]
            client_name = data["client_name"]

            await update.message.reply_text(f"✍️ Overwriting data for *{model}*...", parse_mode="Markdown")

            try:
                result = write_to_list_sheet(model, parts)
                skipped_text = ""
                if result['skipped_list']:
                    skipped_text = "\n\n⚠️ *Not found:*\n" + "\n".join(f"• {s}" for s in result['skipped_list'])
                await update.message.reply_text(
                    f"✅ *Done!* Data overwritten in LIST sheet.\n\n"
                    f"📋 Model: *{model}*\n"
                    f"🔄 Matched by name+code: *{result['updated']}*\n"
                    f"🔍 Matched by code only: *{result['matched_by_code']}*\n"
                    f"⏭ Not found (skipped): *{result['skipped']}*"
                    + skipped_text,
                    parse_mode="Markdown"
                )
                # ✅ Генерируем PDF и после перезаписи
                await update.message.reply_text("📄 Generating Checks Sheet PDF...")
                await send_checks_pdf(update, order_num, client_name, model)

            except Exception as e:
                logger.error(f"Write error: {e}")
                await update.message.reply_text(f"❌ Error writing to sheet: {str(e)}")
            return

        elif message_text.upper() in ["NO", "N", "НЕТ"]:
            pending_confirmations.pop(user_id)
            await update.message.reply_text("❌ Cancelled. No data was changed.")
            return

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
        thinking_msg = await update.message.reply_text("⏳ Processing your request...")
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
        await thinking_msg.edit_text(reply)

    except Exception as e:
        logger.error(f"Claude error: {e}")
        await update.message.reply_text("Sorry, I'm having trouble processing this request right now.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: No TELEGRAM_BOT_TOKEN found!")
        return
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
