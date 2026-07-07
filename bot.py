import os
import io
import re
import json
import logging
import base64
import asyncio
import threading
import queue
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from generate_pdf import generate_checks_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SPREADSHEET_ID = "1NNb7CeNl9gU5TXbJTGvx5RsMMvxgPY39J4nWPFSprBI"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

pending_confirmations = {}
pending_photo = {}  # user_id → {order_num, client_name} ожидают фото

flask_app = Flask(__name__)
telegram_app_ref = None  # глобальная ссылка на Application
order_queue = queue.Queue()  # очередь заказов от Make

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

def get_list_rows_for_model(model: str) -> list:
    data = get_sheet_data("LIST")
    if not data:
        return []
    headers = data[0]
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
            quant = float(quant_raw)
        except ValueError:
            continue
        if quant > 0:
            rows.append({"item": item, "size_code": size_code, "quant": quant_raw})
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

    def norm(s):
        return s.replace(chr(0x2019), "'").replace(chr(0x2018), "'").replace(chr(0x201c), '"').replace(chr(0x201d), '"')

    for row_num, row in enumerate(data[1:], start=2):
        item = row[0].strip() if len(row) > 0 else ""
        sc = norm(row[1].strip()) if len(row) > 1 else ""
        sc_clean = sc.replace(' ', '')
        if not item and not sc_clean:
            continue
        index_both[(item.upper(), sc_clean.upper())] = row_num
        if sc_clean and sc_clean.upper() not in index_code:
            index_code[sc_clean.upper()] = row_num

    updated = skipped = matched_by_code = 0
    skipped_list = []

    for part in parts:
        item = part["item"].strip()
        sc_raw = part["size_code"].strip()
        quantity = part["quantity"]
        sc = sc_raw.replace(" ", "")
        kb = (item.upper(), sc.upper())
        if kb in index_both:
            sheet.update_cell(index_both[kb], model_col_index + 1, quantity)
            updated += 1
        elif sc.upper() in index_code:
            sheet.update_cell(index_code[sc.upper()], model_col_index + 1, quantity)
            matched_by_code += 1
        else:
            skipped += 1
            skipped_list.append(f"{item} | {sc_raw}")

    return {"updated": updated, "matched_by_code": matched_by_code,
            "skipped": skipped, "skipped_list": skipped_list, "column_existed": column_existed}

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

async def send_checks_pdf_to_chat(bot: Bot, chat_id: str, order_num: str, client_name: str, model: str):
    rows = get_list_rows_for_model(model)
    if not rows:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Model *{model}* not found in LIST sheet.", parse_mode="Markdown")
        return
    pdf_bytes = generate_checks_pdf(order_num=order_num, client_name=client_name, model=model, rows=rows)
    filename = f"{order_num}_{client_name}_{model}_Checks.pdf"
    await bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(pdf_bytes),
        filename=filename,
        caption=f"📋 *Checks Sheet* — {order_num} {client_name} | {model}\n_{len(rows)} parts_",
        parse_mode="Markdown"
    )
    logger.info(f"Checks Sheet PDF sent: {filename} ({len(rows)} parts)")

async def send_checks_pdf(update, order_num, client_name, model):
    rows = get_list_rows_for_model(model)
    if not rows:
        await update.message.reply_text(f"⚠️ Model *{model}* not found in LIST sheet.", parse_mode="Markdown")
        return
    pdf_bytes = generate_checks_pdf(order_num=order_num, client_name=client_name, model=model, rows=rows)
    filename = f"{order_num}_{client_name}_{model}_Checks.pdf"
    await update.message.reply_document(
        document=io.BytesIO(pdf_bytes),
        filename=filename,
        caption=f"📋 *Checks Sheet* — {order_num} {client_name} | {model}\n_{len(rows)} parts_",
        parse_mode="Markdown"
    )

# ─── FLASK WEBHOOK ────────────────────────────────────────────────────────────

@flask_app.route("/webhook/new_order", methods=["POST"])
def webhook_new_order():
    """
    Принимает два формата от Make:
    1. {"data": "2699|Spicer|12x20EX"} или {"data": "2699-Spicer 12x20EX"}
    2. {"order_num": "2699", "client_name": "Spicer", "model": "12x20EX"}
    """
    try:
        data = request.get_json(force=True)

        if "data" in data:
            raw = data["data"].strip()
            parts = raw.split("|")
            if len(parts) == 3:
                order_num = parts[0].strip()
                client_name = parts[1].strip()
                model_raw = parts[2].strip()
            else:
                space_idx = raw.find(" ")
                if space_idx == -1:
                    return jsonify({"error": "Invalid data format"}), 400
                left = raw[:space_idx]
                right = raw[space_idx+1:]
                dash_idx = left.find("-")
                if dash_idx == -1:
                    return jsonify({"error": "Invalid data format"}), 400
                order_num = left[:dash_idx]
                client_name = left[dash_idx+1:]
                model_raw = right
            model_match = re.search(r'\d+[x×X]\d+(?:EX2?)?', model_raw, re.IGNORECASE)
            if model_match:
                model = re.sub(r'[×X]', 'x', model_match.group(0))
                model = re.sub(r'ex2?', lambda m: m.group(0).upper(), model, flags=re.IGNORECASE)
            else:
                model = model_raw
        else:
            order_num = data.get("order_num", "").strip()
            client_name = data.get("client_name", "").strip()
            model = data.get("model", "").strip()

        if not order_num or not client_name or not model:
            return jsonify({"error": "Missing fields"}), 400

        chat_id = TELEGRAM_CHAT_ID
        if not chat_id:
            return jsonify({"error": "TELEGRAM_CHAT_ID not set"}), 500

        logger.info(f"Webhook received: {order_num} | {client_name} | {model}")

        # Кладём заказ в очередь — бот обработает в своём event loop
        order_queue.put({
            "order_num": order_num,
            "client_name": client_name,
            "model": model,
            "chat_id": chat_id
        })
        logger.info(f"Order added to queue: {order_num} | {client_name} | {model}")
        return jsonify({"status": "ok", "queued": True}), 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────

def parse_new_order_message(text: str):
    text = text.strip()
    if not text.startswith("NEW_ORDER|"):
        return None
    payload = text[len("NEW_ORDER|"):].strip()
    parts = payload.split("|")
    if len(parts) == 3:
        return {"order_num": parts[0].strip(), "client_name": parts[1].strip(), "model": parts[2].strip()}
    parts2 = payload.split(" ", 1)
    if not parts2 or not parts2[0]:
        return None
    dash_idx = parts2[0].find("-")
    if dash_idx == -1:
        return None
    order_num = parts2[0][:dash_idx]
    client_name = parts2[0][dash_idx+1:]
    remainder = parts2[1] if len(parts2) > 1 else ""
    model_match = re.search(r'\d+[x×X]\d+(?:EX2?)?', remainder, re.IGNORECASE)
    if not model_match:
        return None
    model_raw = model_match.group(0)
    model = re.sub(r'[×X]', 'x', model_raw)
    model = re.sub(r'ex2?', lambda m: m.group(0).upper(), model, flags=re.IGNORECASE)
    return {"order_num": order_num, "client_name": client_name, "model": model}


def extract_model_from_image(image_bytes: bytes) -> dict:
    """
    Отправляет фото PDF спецификации в Claude Vision.
    Возвращает {"model": "10x18EX"} или {"error": "..."}
    """
    import base64
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64
                    }
                },
                {
                    "type": "text",
                    "text": """This is a greenhouse order specification from Cedar-Built Greenhouses.

Find the greenhouse size at the top of the table (e.g. "10' x 18' FREESTANDING GREENHOUSE").
Also check if there is a row called "PORTICO" in the table.

Rules for model name:
- Format: WIDTHxLENGTH (e.g. 10x18)
- If PORTICO row exists → add EX suffix (e.g. 10x18EX)
- If no PORTICO → no suffix (e.g. 10x18)
- Use only numbers and x, no spaces or apostrophes

Return ONLY valid JSON, nothing else:
{"model": "10x18EX"}"""
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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фото PDF спецификации отправленное в Telegram."""
    user_id = update.effective_user.id

    # Проверяем есть ли ожидающие данные заказа
    if user_id not in pending_photo:
        await update.message.reply_text(
            "📋 Please send the order details first as text message:\n"
            "Format: `ORDER_NUMBER CLIENT_NAME`\n"
            "Example: `2755 Bailey`",
            parse_mode="Markdown"
        )
        return

    order_data = pending_photo.pop(user_id)
    order_num = order_data["order_num"]
    client_name = order_data["client_name"]

    await update.message.reply_text("🔍 Reading specification image...")

    try:
        # Берём фото максимального размера
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        # Извлекаем модель через Claude Vision
        result = extract_model_from_image(bytes(image_bytes))

        if "error" in result:
            await update.message.reply_text(f"❌ Could not read the image: {result['error']}")
            return

        model = result["model"]
        logger.info(f"Extracted model from image: {model}")

        await update.message.reply_text(
            f"✅ *Order:* {order_num} | *Client:* {client_name} | *Model:* {model}\n\n"
            f"Generating Checks Sheet PDF...",
            parse_mode="Markdown"
        )

        await send_checks_pdf(update, order_num, client_name, model)

    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        has_data = check_column_has_data(model)
        if has_data:
            pending_confirmations[user_id] = {"model": model, "parts": parts, "order_num": order_num, "client_name": client_name}
            await update.message.reply_text(
                f"⚠️ Column *{model}* already has data.\n\nFound *{len(parts)} parts*.\n\nReply *YES* to overwrite or *NO* to cancel.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"✅ Found *{order_num}* — {client_name} | *{model}* | *{len(parts)} parts*\nWriting to LIST sheet...", parse_mode="Markdown")
            result = write_to_list_sheet(model, parts)
            skipped_text = ""
            if result['skipped_list']:
                skipped_text = "\n\n⚠️ *Not found:*\n" + "\n".join(f"• {s}" for s in result['skipped_list'])
            await update.message.reply_text(
                f"✅ *Done!*\n📋 Model: *{model}*\n🔄 Matched: *{result['updated']}*\n🔍 By code: *{result['matched_by_code']}*\n⏭ Skipped: *{result['skipped']}*" + skipped_text,
                parse_mode="Markdown"
            )
            await update.message.reply_text("📄 Generating Checks Sheet PDF...")
            await send_checks_pdf(update, order_num, client_name, model)
    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def extract_pdf_data_with_claude(pdf_bytes):
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    response = claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_base64}},
            {"type": "text", "text": """Look at PAGE 1 ONLY. Extract:
1. Order number (e.g. "2699")
2. Client name (e.g. "Spicer")
3. Greenhouse model - ONLY size + EX/EX2 suffix, no other words. E.g. "12x20EX SHD" → "12x20EX"
4. All parts: ITEM, SIZE/CODE, QUANT.

Return ONLY valid JSON:
{"order_num": "2699", "client_name": "Spicer", "model": "12x20EX", "parts": [{"item": "BASEWALL", "size_code": "5'-7 1/2\\"", "quantity": 2}]}"""}
        ]}]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

SYSTEM_PROMPT = """You are CBG Manager — an AI assistant for Cedar-Built Greenhouses wood shop in Abbotsford, Canada.
RULES: Always use the data provided. Never invent numbers. Answer in English. Be concise.
{lumber_context}
{parts_context}
"""

user_conversations = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text.strip()
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")

    logger.info(f"Message from {user_name}: {message_text}")

    # Обработка NEW_ORDER от Make (на случай если всё же придёт через Telegram)
    # Проверяем формат "НОМЕР ИМЯ" (напр. "2755 Bailey") — ожидание фото
    order_cmd = re.match(r'^(\d{3,6})\s+([A-Za-z][A-Za-z0-9\s-]{1,30})$', message_text.strip())
    if order_cmd and user_id not in pending_confirmations:
        order_num_cmd = order_cmd.group(1).strip()
        client_name_cmd = order_cmd.group(2).strip()
        pending_photo[user_id] = {"order_num": order_num_cmd, "client_name": client_name_cmd}
        await update.message.reply_text(
            f"✅ Order *{order_num_cmd}* — *{client_name_cmd}* saved.\n\n"
            f"Now send a photo of the PDF specification (page 1 with the parts table).",
            parse_mode="Markdown"
        )
        return

    order_data = parse_new_order_message(message_text)
    if order_data:
        order_num = order_data["order_num"]
        client_name = order_data["client_name"]
        model = order_data["model"]
        await update.message.reply_text(
            f"📋 *New order received!*\n*Order:* {order_num}\n*Client:* {client_name}\n*Model:* {model}\n\nGenerating Checks Sheet PDF...",
            parse_mode="Markdown"
        )
        await send_checks_pdf(update, order_num, client_name, model)
        return

    if user_id in pending_confirmations:
        if message_text.upper() in ["YES", "Y", "ДА"]:
            data = pending_confirmations.pop(user_id)
            try:
                result = write_to_list_sheet(data["model"], data["parts"])
                await update.message.reply_text(f"✅ *Done!* Data overwritten for *{data['model']}*", parse_mode="Markdown")
                await send_checks_pdf(update, data["order_num"], data["client_name"], data["model"])
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {str(e)}")
            return
        elif message_text.upper() in ["NO", "N", "НЕТ"]:
            pending_confirmations.pop(user_id)
            await update.message.reply_text("❌ Cancelled.")
            return

    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": f"[{user_name}, {current_time}]: {message_text}"})
    if len(user_conversations[user_id]) > 10:
        user_conversations[user_id] = user_conversations[user_id][-10:]

    system_instruction = SYSTEM_PROMPT.format(lumber_context=build_lumber_context(), parts_context=build_parts_context())
    system_instruction += f"\nCurrent date/time: {current_date}, {current_time}."

    try:
        thinking_msg = await update.message.reply_text("⏳ Processing your request...")
        response = claude_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=1200,
            system=system_instruction, messages=user_conversations[user_id]
        )
        reply = response.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": reply})
        await thinking_msg.edit_text(reply)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        await update.message.reply_text("Sorry, I'm having trouble right now.")

def run_flask():
    port = int(os.environ.get("FLASK_PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)

def main():
    global telegram_app_ref
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: No TELEGRAM_BOT_TOKEN found!")
        return

    app = Application.builder().token(token).build()
    telegram_app_ref = app

    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask webhook server started")

    # Запускаем обработчик очереди заказов
    async def process_order_queue():
        while True:
            try:
                order = order_queue.get_nowait()
                try:
                    bot = app.bot
                    await bot.send_message(
                        chat_id=order["chat_id"],
                        text=f"📋 *New order received!*\n*Order:* {order['order_num']}\n*Client:* {order['client_name']}\n*Model:* {order['model']}\n\nGenerating Checks Sheet PDF...",
                        parse_mode="Markdown"
                    )
                    await send_checks_pdf_to_chat(bot, order["chat_id"], order["order_num"], order["client_name"], order["model"])
                except Exception as e:
                    logger.error(f"Queue process error: {e}")
            except queue.Empty:
                pass
            await asyncio.sleep(1)

    app.job_queue  # ensure job queue exists
    
    async def post_init(application):
        asyncio.create_task(process_order_queue())
    
    app.post_init = post_init

    logger.info("CBG Manager Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
