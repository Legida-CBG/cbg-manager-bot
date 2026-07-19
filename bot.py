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
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
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
pending_photo = {}  # user_id → {order_num, client_name, door_config} ожидают фото
pending_door_config = {}  # user_id → {order_num, client_name} ожидают ответа Single/Double
pending_pdf_choice = {}  # user_id → {order_num, client_name, door_config} ожидают ответа "есть ли PDF"
pending_manual_entry = {}  # user_id → {order_num, client_name, door_config, step, ...} ручной ввод без фото

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
    Возвращает {"model": "10x18EX", "front_doors": 1, "back_doors": 2, "window": false}
    или {"error": "..."}
    """
    import base64
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
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

Also look at the door line items in the table (ITEM column):
- Count total FRONT doors: sum quantities of any row referring to a front door
  (e.g. "REGULAR FRONT DOOR", "ADDITIONAL FRONT DOOR", "DUTCH FRONT DOOR",
  "DUTCH FRONT DOUBLE DOORS", "DOOR (SINGLE)" if it is on the front end, etc.)
- Count total BACK doors: sum quantities of any row referring to a back door
  (e.g. "DUTCH BACK DOUBLE DOORS", "REGULAR BACK DOOR", "ADDITIONAL BACK DOOR", etc.)
- Check if the word "WINDOW" appears anywhere in the ITEM or description column
  (e.g. "DOUBLE DUTCH WINDOW UPGRADE").

Return ONLY valid JSON, nothing else:
{"model": "10x18EX", "front_doors": 1, "back_doors": 2, "window": false}"""
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


def get_greenhouse_width(model: str):
    """Извлекает ширину теплицы из названия модели, напр. '12x18EX' -> 12"""
    m = re.match(r'(\d+)\s*[xX]\s*\d+', model.strip())
    if m:
        return int(m.group(1))
    return None


def _replace_code_prefix(code: str, old: str, new: str) -> str:
    """Заменяет old на new внутри code (без учёта регистра), сохраняя остальной текст."""
    idx = code.upper().find(old.upper())
    if idx == -1:
        return code
    return code[:idx] + new + code[idx + len(old):]


def _double_quant(quant):
    """Удваивает количество (quant может быть строкой типа '2' или '2.5')."""
    try:
        if '.' in str(quant):
            val = float(quant) * 2
            return str(val) if val != int(val) else str(int(val))
        return str(int(quant) * 2)
    except (ValueError, TypeError):
        return quant


def apply_door_config_substitutions(rows: list, width, door_config: str, ea: bool, window: bool) -> list:
    """
    Применяет замены GW / EP / Lintel Beam / GBX деталей на основе конфигурации дверей.

    rows: список {"item":, "size_code":, "quant":} из LIST sheet
    width: ширина теплицы (8, 10, 12, 14) или None
    door_config: "single" или "double" — ответ пользователя на кнопки (управляет GW/EP/Lintel Beam/GBX)
    ea: bool — double с ОБЕИХ сторон (авто-определено по фото); влияет только на GW/EP
    window: bool — есть окно (авто-определено по фото); влияет только на Lintel Beam
    """
    result = []
    for row in rows:
        item = row["item"]
        code = row["size_code"]
        quant = row["quant"]
        code_clean = code.replace(" ", "").upper()
        new_code = code
        new_quant = quant
        skip = False

        # ---- GW parts — только модели шириной 12' ----
        if width == 12 and code_clean.startswith("GW47"):
            if ea or door_config == "double":
                new_code = _replace_code_prefix(code, "GW47", "GW35")

        elif width == 12 and code_clean.startswith("GW36"):
            if ea:
                skip = True  # GW36 удаляется полностью, GW60 НЕ добавляется
            elif door_config == "double":
                new_code = _replace_code_prefix(code, "GW36", "GW60")

        # ---- EP parts — модели шириной 10' ----
        elif width == 10 and code_clean.startswith("EP70"):
            is_ld_rd = "LD" in code_clean or "RD" in code_clean
            if ea:
                if is_ld_rd:
                    new_code = _replace_code_prefix(code, "EP70", "EP66")
                    new_quant = _double_quant(quant)
                else:
                    skip = True  # EP70-L/R удаляется полностью
            elif door_config == "double":
                new_code = _replace_code_prefix(code, "EP70", "EP66")

        # ---- EP parts — модели шириной 12' / 14' ----
        elif width in (12, 14) and code_clean.startswith("EP76"):
            is_ld_rd = "LD" in code_clean or "RD" in code_clean
            if ea:
                if is_ld_rd:
                    new_code = _replace_code_prefix(code, "EP76", "EP70")
                    new_quant = _double_quant(quant)
                else:
                    skip = True  # EP76-L/R удаляется полностью
            elif door_config == "double":
                new_code = _replace_code_prefix(code, "EP76", "EP70")

        # ---- Lintel Beam — модели шириной 12' / 14' ----
        elif width in (12, 14) and code_clean == "LBS-1":
            if door_config == "double":
                new_code = "LB D-1"

        elif width in (12, 14) and code_clean == "LBS-2":
            if door_config == "double" and window:
                new_code = "LB D-1N"
            elif door_config == "double" and not window:
                new_code = "LB D-2"
            elif door_config == "single" and window:
                new_code = "LB S-1N"
            # single, без окна — без изменений

        # ---- Lintel Beam — модели шириной 8' / 10' (нет Double-варианта) ----
        elif width in (8, 10) and code_clean == "LBS-2":
            if window:
                new_code = "LB S-1N"
            # LB S-1 на 8'/10' никогда не меняется

        # ---- EX Gable Batton (GBX) — модели шириной 12' / 14' ----
        # EA НЕ влияет на это правило — важен только ответ Single/Double.
        # На 8'/10' GBX всегда остаётся GBX-S (сюда не заходит).
        elif width in (12, 14) and code_clean.startswith("GBX-S"):
            if door_config == "double":
                new_code = _replace_code_prefix(code, "GBX-S", "GBX-T")
            # single — без изменений, остаётся GBX-S

        if skip:
            continue
        new_row = dict(row)
        new_row["size_code"] = new_code
        new_row["quant"] = new_quant
        result.append(new_row)

    return result


def _fmt_qty(q):
    """Форматирует число как строку — целое без .0, иначе как есть."""
    return str(int(q)) if q == int(q) else str(q)


def recalculate_lintel_posts(rows: list) -> list:
    """
    Пересчитывает количество Lintel Posts (LP-T / LP-S) на основе итоговых
    кодов Lintel Beam (после apply_door_config_substitutions).

    Правило:
    - Front балка: 1 Lintel Post
    - Back балка: 1 Lintel Post
    - Midspan балка: 2 Lintel Post на каждую балку
    - Если код балки начинается с "LB D" (double) -> Lintel Post Tall (LP-T)
    - Если код балки начинается с "LB S" (single) -> Lintel Post Short (LP-S)
    """
    lp_t_total = 0
    lp_s_total = 0
    has_lintel_beam = False

    for row in rows:
        item_upper = row["item"].upper()
        if "LINTEL BEAM" not in item_upper:
            continue
        has_lintel_beam = True

        code_clean = row["size_code"].replace(" ", "").upper()
        try:
            qty = float(row["quant"])
        except (ValueError, TypeError):
            qty = 1

        multiplier = 2 if "MIDSPAN" in item_upper else 1

        if code_clean.startswith("LBD"):
            lp_t_total += qty * multiplier
        elif code_clean.startswith("LBS"):
            lp_s_total += qty * multiplier

    if not has_lintel_beam:
        return rows

    result = []
    lp_t_found = False
    lp_s_found = False

    for row in rows:
        code_clean = row["size_code"].replace(" ", "").upper()
        if code_clean == "LP-T":
            lp_t_found = True
            if lp_t_total <= 0:
                continue
            new_row = dict(row)
            new_row["quant"] = _fmt_qty(lp_t_total)
            result.append(new_row)
        elif code_clean == "LP-S":
            lp_s_found = True
            if lp_s_total <= 0:
                continue
            new_row = dict(row)
            new_row["quant"] = _fmt_qty(lp_s_total)
            result.append(new_row)
        else:
            result.append(row)

    if not lp_t_found and lp_t_total > 0:
        result.append({"item": "LINTEL POST - TALL", "size_code": "LP-T", "quant": _fmt_qty(lp_t_total)})
    if not lp_s_found and lp_s_total > 0:
        result.append({"item": "LINTEL POST - SHORT", "size_code": "LP-S", "quant": _fmt_qty(lp_s_total)})

    return result


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
    door_config = order_data.get("door_config", "single")

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
        front_doors = result.get("front_doors", 1)
        back_doors = result.get("back_doors", 1)
        window = bool(result.get("window", False))
        ea = front_doors > 1 and back_doors > 1
        width = get_greenhouse_width(model)

        logger.info(
            f"Extracted model from image: {model} | door_config={door_config} | "
            f"front_doors={front_doors} | back_doors={back_doors} | ea={ea} | window={window}"
        )

        config_summary = "EA (double both sides)" if ea else door_config.capitalize()
        window_summary = " + Window" if window else ""

        await update.message.reply_text(
            f"✅ *Order:* {order_num} | *Client:* {client_name} | *Model:* {model}\n"
            f"🚪 *Door config:* {config_summary}{window_summary}\n\n"
            f"Generating Checks Sheet PDF...",
            parse_mode="Markdown"
        )

        rows = get_list_rows_for_model(model)
        if not rows:
            await update.message.reply_text(f"⚠️ Model *{model}* not found in LIST sheet.", parse_mode="Markdown")
            return
        rows = apply_door_config_substitutions(rows, width, door_config, ea, window)
        rows = recalculate_lintel_posts(rows)

        pdf_bytes = generate_checks_pdf(order_num=order_num, client_name=client_name, model=model, rows=rows)
        filename = f"{order_num}_{client_name}_{model}_Checks.pdf"
        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📋 *Checks Sheet* — {order_num} {client_name} | {model}\n_{len(rows)} parts_",
            parse_mode="Markdown"
        )

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

    # Ручной ввод размера теплицы (когда нет PDF)
    if user_id in pending_manual_entry and pending_manual_entry[user_id].get("step") == "await_size":
        size_match = re.match(r'^(\d+)\s*[xX]\s*(\d+)$', message_text.strip())
        if not size_match:
            await update.message.reply_text(
                "⚠️ Please enter the size in format WIDTHxLENGTH, e.g. `14x24`",
                parse_mode="Markdown"
            )
            return
        pending_manual_entry[user_id]["width"] = int(size_match.group(1))
        pending_manual_entry[user_id]["length"] = int(size_match.group(2))
        pending_manual_entry[user_id]["step"] = "await_suffix"
        await update.message.reply_text(
            "Does this model have an EX suffix?",
            reply_markup=SUFFIX_KEYBOARD
        )
        return

    # Обработка NEW_ORDER от Make (на случай если всё же придёт через Telegram)
    # Проверяем формат "НОМЕР ИМЯ" (напр. "2755 Bailey") — теперь спрашиваем Single/Double перед фото
    order_cmd = re.match(r'^(\d{3,6})\s+([A-Za-z][A-Za-z0-9\s-]{1,30})$', message_text.strip())
    if order_cmd and user_id not in pending_confirmations:
        order_num_cmd = order_cmd.group(1).strip()
        client_name_cmd = order_cmd.group(2).strip()
        pending_door_config[user_id] = {"order_num": order_num_cmd, "client_name": client_name_cmd}
        await update.message.reply_text(
            f"✅ Order *{order_num_cmd}* — *{client_name_cmd}* saved.",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            "Is Lintel Beam double in this order?",
            reply_markup=DOOR_CONFIG_KEYBOARD
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

# ─── BUTTON MENU ──────────────────────────────────────────────────────────────

# Главное меню — 4 кнопки
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔧 Action", callback_data="menu_action")],
    [InlineKeyboardButton("ℹ️ Info", callback_data="menu_info")],
    [InlineKeyboardButton("📦 Request", callback_data="menu_request")],
    [InlineKeyboardButton("🚚 Shipped", callback_data="menu_shipped")],
])

# Подменю кнопки Action — 3 кнопки
ACTION_SUBMENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🟢 Пришёл на работу", callback_data="action_checkin")],
    [InlineKeyboardButton("🔨 Изготовил", callback_data="action_produced")],
    [InlineKeyboardButton("🔴 Ушёл с работы", callback_data="action_checkout")],
    [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")],
])

# Кнопки Single/Double для Lintel Beam (задаются перед запросом фото спецификации)
DOOR_CONFIG_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Double", callback_data="doorcfg_double")],
    [InlineKeyboardButton("Single", callback_data="doorcfg_single")],
])

# Есть ли PDF спецификация?
HAS_PDF_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Yes", callback_data="haspdf_yes")],
    [InlineKeyboardButton("No", callback_data="haspdf_no")],
])

# Суффикс модели (ручной ввод)
SUFFIX_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("None", callback_data="suffix_none")],
    [InlineKeyboardButton("EX", callback_data="suffix_ex")],
    [InlineKeyboardButton("EX2", callback_data="suffix_ex2")],
])

# Есть ли окно (ручной ввод)
WINDOW_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Yes", callback_data="window_yes")],
    [InlineKeyboardButton("No", callback_data="window_no")],
])

# EA — двери с двух сторон (ручной ввод)
EA_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Yes", callback_data="ea_yes")],
    [InlineKeyboardButton("No", callback_data="ea_no")],
])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — показывает главное меню с 4 кнопками."""
    await update.message.reply_text(
        "👋 CBG Manager — main menu:",
        reply_markup=MAIN_MENU
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на все inline-кнопки."""
    query = update.callback_query
    await query.answer()  # убирает "часики" на кнопке в Telegram
    data = query.data
    user_id = query.from_user.id

    if data == "menu_action":
        await query.edit_message_text("🔧 Action — choose:", reply_markup=ACTION_SUBMENU)

    elif data == "menu_back":
        await query.edit_message_text("👋 CBG Manager — main menu:", reply_markup=MAIN_MENU)

    elif data == "menu_info":
        await query.edit_message_text("ℹ️ Info — 🚧 in development (coming next).")

    elif data == "menu_request":
        await query.edit_message_text("📦 Request — 🚧 in development (coming next).")

    elif data == "menu_shipped":
        await query.edit_message_text("🚚 Shipped — 🚧 in development (coming next).")

    elif data == "action_checkin":
        await query.edit_message_text("🟢 Check-in — 🚧 in development (coming next).")

    elif data == "action_produced":
        await query.edit_message_text("🔨 Produced — 🚧 in development (coming next).")

    elif data == "action_checkout":
        await query.edit_message_text("🔴 Check-out — 🚧 in development (coming next).")

    elif data in ("doorcfg_double", "doorcfg_single"):
        if user_id not in pending_door_config:
            await query.edit_message_text(
                "⚠️ No pending order found. Please send the order details again:\n"
                "Format: `ORDER_NUMBER CLIENT_NAME`",
                parse_mode="Markdown"
            )
            return
        order_info = pending_door_config.pop(user_id)
        door_config = "double" if data == "doorcfg_double" else "single"
        pending_pdf_choice[user_id] = {
            "order_num": order_info["order_num"],
            "client_name": order_info["client_name"],
            "door_config": door_config
        }
        await query.edit_message_text(
            f"🚪 Lintel Beam: *{door_config.capitalize()}*\n\n"
            f"Do you have the PDF specification for this order?",
            parse_mode="Markdown",
            reply_markup=HAS_PDF_KEYBOARD
        )

    elif data in ("haspdf_yes", "haspdf_no"):
        if user_id not in pending_pdf_choice:
            await query.edit_message_text(
                "⚠️ No pending order found. Please send the order details again:\n"
                "Format: `ORDER_NUMBER CLIENT_NAME`",
                parse_mode="Markdown"
            )
            return
        order_info = pending_pdf_choice.pop(user_id)
        if data == "haspdf_yes":
            pending_photo[user_id] = order_info
            await query.edit_message_text(
                "Now send a photo of the PDF specification (page 1 with the parts table)."
            )
        else:
            pending_manual_entry[user_id] = {
                "order_num": order_info["order_num"],
                "client_name": order_info["client_name"],
                "door_config": order_info["door_config"],
                "step": "await_size"
            }
            await query.edit_message_text(
                "Please enter the greenhouse size as WIDTHxLENGTH (e.g. `14x24`)",
                parse_mode="Markdown"
            )

    elif data in ("suffix_none", "suffix_ex", "suffix_ex2"):
        if user_id not in pending_manual_entry or pending_manual_entry[user_id].get("step") != "await_suffix":
            return
        suffix_map = {"suffix_none": "", "suffix_ex": "EX", "suffix_ex2": "EX2"}
        pending_manual_entry[user_id]["suffix"] = suffix_map[data]
        pending_manual_entry[user_id]["step"] = "await_window"
        await query.edit_message_text(
            "Is there a window in this order?",
            reply_markup=WINDOW_KEYBOARD
        )

    elif data in ("window_yes", "window_no"):
        if user_id not in pending_manual_entry or pending_manual_entry[user_id].get("step") != "await_window":
            return
        pending_manual_entry[user_id]["window"] = (data == "window_yes")
        pending_manual_entry[user_id]["step"] = "await_ea"
        await query.edit_message_text(
            "Is this EA — double doors on both sides (front AND back)?",
            reply_markup=EA_KEYBOARD
        )

    elif data in ("ea_yes", "ea_no"):
        if user_id not in pending_manual_entry or pending_manual_entry[user_id].get("step") != "await_ea":
            return
        entry = pending_manual_entry.pop(user_id)
        ea = (data == "ea_yes")
        width = entry["width"]
        length = entry["length"]
        suffix = entry["suffix"]
        window = entry["window"]
        door_config = entry["door_config"]
        order_num = entry["order_num"]
        client_name = entry["client_name"]
        model = f"{width}x{length}{suffix}"

        await query.edit_message_text(
            f"✅ *Order:* {order_num} | *Client:* {client_name} | *Model:* {model}\n"
            f"🚪 *Door config:* {'EA (double both sides)' if ea else door_config.capitalize()}"
            f"{' + Window' if window else ''}\n\n"
            f"Generating Checks Sheet PDF...",
            parse_mode="Markdown"
        )

        rows = get_list_rows_for_model(model)
        if not rows:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"⚠️ Model *{model}* not found in LIST sheet.",
                parse_mode="Markdown"
            )
            return
        rows = apply_door_config_substitutions(rows, width, door_config, ea, window)
        rows = recalculate_lintel_posts(rows)

        pdf_bytes = generate_checks_pdf(order_num=order_num, client_name=client_name, model=model, rows=rows)
        filename = f"{order_num}_{client_name}_{model}_Checks.pdf"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📋 *Checks Sheet* — {order_num} {client_name} | {model}\n_{len(rows)} parts_",
            parse_mode="Markdown"
        )


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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
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
