import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Clients
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Google Sheets setup
def get_sheets_client():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_parts_data():
    """Load all parts and quantities from Cedar-Built spreadsheet"""
    try:
        gc = get_sheets_client()
        sheet = gc.open("Cedar-Built ").sheet1
        data = sheet.get_all_values()
        return data
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return None

def build_parts_context():
    """Convert spreadsheet data to text context for Claude"""
    data = get_parts_data()
    if not data:
        return "Could not load parts data from Google Sheets."
    
    headers = data[0]  # First row: Item, Size/Code, 6x8, 6x10, ...
    sizes = headers[2:]  # Greenhouse sizes start from column 3
    
    context = "CEDAR-BUILT GREENHOUSE PARTS LIST:\n\n"
    context += f"Available greenhouse sizes: {', '.join(sizes)}\n\n"
    context += "Parts per greenhouse size (Item | Code | Size: Quantity):\n"
    
    for row in data[1:]:
        if len(row) >= 2 and row[0]:
            item = row[0]
            code = row[1] if len(row) > 1 else ""
            quantities = []
            for i, qty in enumerate(row[2:], 0):
                if qty and qty.strip():
                    if i < len(sizes):
                        quantities.append(f"{sizes[i]}:{qty}")
            if quantities:
                context += f"- {item} ({code}): {', '.join(quantities)}\n"
    
    return context

# System prompt for Claude
SYSTEM_PROMPT = """You are CBG Manager — the AI assistant for Cedar-Built Greenhouses, a woodworking shop in Abbotsford, Canada.

You help shop staff with:
1. Providing parts lists for specific greenhouse sizes
2. Answering questions about parts, codes, and quantities
3. Tracking work hours (arrivals, departures, lunch breaks)
4. Inventory management

Always respond in English. Be clear, concise, and practical.

When someone asks about parts for a greenhouse size (e.g. "parts for 10x12" or "what do I need for 8x10"), 
provide a clear formatted list with item name, code, and quantity.

For work time tracking:
- When staff says "I'm here", "arrived", "at work" → confirm arrival and note the time
- When staff says "going home", "leaving", "done for the day" → confirm departure
- When staff says "lunch", "lunch break" → confirm lunch break start

{parts_context}
"""

# Store conversation history per user
user_conversations = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Staff"
    message_text = update.message.text
    current_time = datetime.now().strftime("%I:%M %p")
    current_date = datetime.now().strftime("%B %d, %Y")
    
    logger.info(f"Message from {user_name}: {message_text}")
    
    # Initialize conversation history
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    
    # Add user message to history
    user_conversations[user_id].append({
        "role": "user",
        "content": f"[{user_name}, {current_time}]: {message_text}"
    })
    
    # Keep only last 20 messages to manage context
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]
    
    # Load parts data
    parts_context = build_parts_context()
    system = SYSTEM_PROMPT.format(parts_context=parts_context)
    system += f"\n\nCurrent date and time: {current_date}, {current_time}"
    
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=user_conversations[user_id]
        )
        
        reply = response.content[0].text
        
        # Add assistant response to history
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply
        })
        
        await update.message.reply_text(reply)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please try again.")

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("CBG Manager Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
