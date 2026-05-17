import os
import json
import logging
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SPREADSHEET_ID = "1NNb7CeNl9gU5TXbJTGvx5RsMMvxgPY39J4nWPFSprBl"

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

def update_cell_direct(sheet_name, row_number, col_number, value):
    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds)
        col_letter = chr(64 + col_number)
        range_name = f"{sheet_name}!{col_letter}{row_number}"
        body = {"values": [[value]]}
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption="RAW",
            body=body
        ).execute()
        logger.info(f"Updated {range_name} = {value}")
        return True
    except Exception as e:
        logger.error(f"Direct update error: {e}")
        return False

def update_lumber_stock(message_text, qty_change):
    data = get_sheet_data("LUMBER")
    if not data:
        return "ERROR_CONN|Could not connect to database."

    words_to_remove = ['took', 'added', 'minus', 'plus', 'of', 'from',
                       'warehouse', 'stock', 'to', 'the', 'items', 'pieces', 'pcs',
                       'received', 'used', 'pulled', 'got', 'i', 'we']
    clean_text = message_text.lower()
    for w in words_to_remove:
        clean_text = re.sub(r'\b' + w + r'\b', '', clean_text)
    clean_text = re.sub(r'\b\d+\b', '', clean_text)

    search_tokens = [t.strip() for t in re.split(r'[\s,]+', clean_text)
                     if len(t.strip()) >= 2 or "'" in t or '"' in t]

    if not search_tokens:
        return "ERROR_TOKENS|Could not extract item description."

    best_rows = []
    for idx, row in enumerate(data[1:], start=2):
        if len(row) < 3:
            continue
        full_row_name = f"{row[0]} {row[1]} {row[2]}".strip().lower()
        matches = sum(1 for token in search_tokens if token in full_row_name)
        if matches > 0:
            best_rows.append((matches, idx, f"{row[0]} {row[1]} @ {row[2]}'"))

    best_rows.sort(key=lambda x: x[0], reverse=True)

    if not best_rows:        
