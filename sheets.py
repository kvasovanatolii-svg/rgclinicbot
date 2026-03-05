import json
import gspread
from google.oauth2.service_account import Credentials
from config import SERVICE_JSON, SPREADSHEET_ID

def client():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(
        json.loads(SERVICE_JSON),
        scopes=scopes
    )

    return gspread.authorize(creds)


def sheet(name):

    gc = client()

    return gc.open_by_key(SPREADSHEET_ID).worksheet(name)


def records(name):

    try:
        return sheet(name).get_all_records()
    except:
        return []
