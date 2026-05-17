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

def get_spreadsheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    gc = gspread.service_account_from_dict(creds_dict)
    return gc.open("Cedar-Built")

def get_sheet_data(sheet_name):
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_name)
        data = sheet.get_all_values()
        logger.info(f"SUCCESS: Loaded {len(data)} rows from '{sheet_name}'")
        return data
    except Exception as e:
        logger.error(f"Sheets error ({sheet_name}): {e}")
        return None

def update_lumber_stock(lumber, category, length, change, operation):
    """
    Update IN_STOK for a specific lumber item.
    operation: 'add' or 'subtract'
    Returns: (success, message)
    """
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet("Lumber")
        data = sheet.get_all_values()
        
        # Find the row
        for i, row in enumerate(data[1:], start=2):
            if len(row) < 4:
                continue
            row_lumber = row[0].strip().lower()
            row_category = row[1].strip().lower()
            row_length = row[2].strip().lower().replace("'", "").replace('"', '')
            
            search_lumber = lumber.strip().lower()
            search_category = category.strip().lower()
            search_length = length.strip().lower().replace("'", "").replace('"', '')
            
            if row_lumber == search_lumber and row_category == search_category and row_length == search_length:
                current = int(row[3]) if row[3].strip().isdigit() else 0
                
                if operation == 'add':
                    new_value = current + change
                    action = f"Added {change} pcs"
                else:
                    if current < change:
                        return False, f"Cannot subtract {change} — only {current} in stock!"
                    new_value = current - change
                    action = f"Removed {change} pcs"
                
                # Update cell D (column 4 = index 4 in gspread 1-based)
                sheet.update_cell(i, 4, new_value)
                logger.info(f"Updated {lumber} {category} @ {length}: {current} → {new_value}")
                return True, f"{action}. {lumber} {category} @ {length}': {current} → {new_value} pcs"
        
        return False, f"Item not found: {lumber} {category} @ {length}'"
        
    except Exception as e:
        logger.error(f"Update error: {e}")
        return False, f"Error updating stock: {e}"

def build_lumber_context():
    data = get_sheet_data("Lumber")
    if not data:
        return "ERROR: Could not load lumber data."
    
    table_text = "LUMBER INVENTORY (columns: LUMBER | CATEGORY | LENGTH | IN_STOK | MIN_STOCK | MIN_ORDER):\n\n"
    for row in data[1:]:
        if not row or not row[0].strip():
            continue
        lumber = row[0].strip()
        category = row[1].strip() if len(row) > 1 else ""
        length = row[2].strip() if len(row) > 2 else ""
        in_stok = row[3].strip() if len(row) > 3 else "0"
        min_stock = row[4].strip() if len(row) > 4 else "0"
        
        low = ""
        try:
            if int(in_stok) < int(min_stock):
                low = " ⚠️ LOW"
        except:
            pass
        
        table_text += f"{lumber} {category} @ {length}': IN_STOK={in_stok} | MIN_STOCK={min_stock}{low}\n"
    
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

You manage lumber inventory and greenhouse parts information.

LUMBER INVENTORY RULES:
- Column IN_STOK = current quantity on hand
- Column MIN_STOCK = minimum threshold
- Show each item with its IN_STOK value
- Format: [Lumber] [Category] @ [Length]': [IN_STOK] pcs (Min: [MIN_STOCK])
- Add ⚠️ LOW if IN_STOK < MIN_STOCK, add ✅ if above

STOCK UPDATE DETECTION:
When staff mentions receiving or using lumber, extract:
1. Lumber size (e.g. 2x4, 2x6)
2. Category (e.g. STK, Clear, SPF)
3. Length (e.g. 6', 8')
4. Quantity (number)
5. Operation: RECEIVED/ADD or USED/TOOK/SUBTRACT

Examples of ADD phrases: "received", "got", "delivered", "added", "came in"
Examples of SUBTRACT phrases: "used", "took", "pulled", "consumed", "taken"

When you detect a stock update, respond with EXACTLY this format on the first line:
STOCK_UPDATE: [ADD or SUBTRACT] | [lumber] | [category] | [length] | [quantity]

Example: STOCK_UPDATE: SUBTRACT | 2x4 | STK | 6 | 100

Then confirm what you understood in a friendly message.

GREENHOUSE PARTS RULES:
- Find the requested greenhouse size in COLUMNS
- List only items with quantity > 0
- NEVER invent quantities
- If size not found, list available sizes

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
        
        # Check if bot detected a stock update
        if reply.startswith("STOCK_UPDATE:"):
            lines = reply.split('\n', 1)
            update_line = lines[0]
            user_reply = lines[1].strip() if len(lines) > 1 else "Stock updated!"
            
            # Parse: STOCK_UPDATE: ADD | 2x4 | STK | 6 | 100
            try:
                parts = update_line.replace("STOCK_UPDATE:", "").strip().split("|")
                operation = parts[0].strip().upper()
                lumber = parts[1].strip()
                category = parts[2].strip()
                length = parts[3].strip()
                quantity = int(parts[4].strip())
                
                op = 'add' if operation == 'ADD' else 'subtract'
                success, msg = update_lumber_stock(lumber, category, length, quantity, op)
                
                if success:
                    await update.message.reply_text(f"✅ {msg}\n\n{user_reply}")
                else:
                    await update.message.reply_text(f"❌ {msg}\n\n{user_reply}")
            except Exception as e:
                logger.error(f"Parse error: {e}")
                await update.message.reply_text(user_reply)
        else:
            await update.message.reply_text(reply)
        
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply
        })

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
