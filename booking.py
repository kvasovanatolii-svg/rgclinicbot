from sheets import sheet, records
from config import SHEET_SCHEDULE

def free_slots():

    rows = records(SHEET_SCHEDULE)

    result = []

    for r in rows:

        if r.get("status") == "FREE":

            result.append(r)

    return result


def book_slot(slot_id, name, phone):

    ws = sheet(SHEET_SCHEDULE)

    rows = ws.get_all_records()

    for i, r in enumerate(rows, start=2):

        if r["slot_id"] == slot_id and r["status"] == "FREE":

            ws.update_cell(i, 8, "BOOKED")
            ws.update_cell(i, 9, name)
            ws.update_cell(i, 10, phone)

            return True

    return False
