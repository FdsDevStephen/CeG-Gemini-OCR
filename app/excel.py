"""Keep Observations_<Model>.xlsx in sync with the <grievance_id>Observation.json files in the same folder.

New grievances are appended as rows; a grievance that is already in the sheet
(e.g. re-checked with --force) has its row updated in place. Rows already in
the sheet are never removed, so the workbook keeps growing across runs.

Used by app.observation: each observation is written to the sheet as soon as it
is produced, and the whole observation folder is re-synced at the end of the run.
"""

import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


SHEET = "Observations"
SUMMARY_SHEET = "Summary"

# (header, observation key, column width)
COLUMNS = [
    ("Case ID", "case_id", 12),
    ("Resolved?", "is_resolved", 11),
    ("Case Summary", "case_summary", 50),
    ("Final Action Taken", "final_action_taken", 50),
    ("ATR Key Sentence (Kannada)", "atr_key_sentence", 50),
    ("ATR Key Sentence (English)", "atr_key_sentence_english", 50),
]

RESOLVED_COLOURS = {"Yes": "C6EFCE", "No": "FFC7CE"}

_thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def _style_header(cell):
    cell.fill = HEADER_FILL
    cell.font = HEADER_FONT
    cell.border = BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _new_observation_sheet(wb):
    ws = wb.create_sheet(SHEET, 0)
    for col, (header, _, width) in enumerate(COLUMNS, start=1):
        _style_header(ws.cell(1, col, header))
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "B2"
    return ws


def _cell_value(key, observation):
    value = observation.get(key)
    if key == "is_resolved":
        return "Yes" if value else "No"
    if key == "case_id" and str(value).isdigit():
        return int(value)
    return value


def _write_row(ws, row, observation):
    for col, (_, key, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row, col, _cell_value(key, observation))
        cell.border = BORDER
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    resolved_cell = ws.cell(row, 2)
    resolved_cell.fill = PatternFill("solid", fgColor=RESOLVED_COLOURS[resolved_cell.value])
    resolved_cell.font = Font(bold=True)


def _rebuild_summary(wb):
    if SUMMARY_SHEET in wb.sheetnames:
        del wb[SUMMARY_SHEET]
    s = wb.create_sheet(SUMMARY_SHEET)
    for col, header in enumerate(("Resolved?", "Count", "Share"), start=1):
        _style_header(s.cell(1, col, header))

    total_row = len(RESOLVED_COLOURS) + 2
    for row, (answer, colour) in enumerate(RESOLVED_COLOURS.items(), start=2):
        s.cell(row, 1, answer).fill = PatternFill("solid", fgColor=colour)
        s.cell(row, 2, f"=COUNTIF({SHEET}!$B:$B,A{row})")
        s.cell(row, 3, f"=IF($B${total_row}=0,0,B{row}/$B${total_row})").number_format = "0%"
    s.cell(total_row, 1, "Total").font = Font(bold=True)
    s.cell(total_row, 2, f"=SUM(B2:B{total_row - 1})").font = Font(bold=True)
    s.cell(total_row, 3, f"=IF($B${total_row}=0,0,1)").number_format = "0%"

    for row in range(2, total_row + 1):
        for col in (1, 2, 3):
            s.cell(row, col).border = BORDER
    s.column_dimensions["A"].width = 26
    s.column_dimensions["B"].width = 10
    s.column_dimensions["C"].width = 10


def workbook_path(observation_dir: Path) -> Path:
    """Observation/Gemini -> Observation/Gemini/Observations_Gemini.xlsx"""
    return observation_dir / f"Observations_{observation_dir.name}.xlsx"


def update_workbook(observations, workbook_path: Path) -> tuple[int, int]:
    """Append new observations / update existing ones. Returns (added, updated)."""
    if workbook_path.exists():
        wb = load_workbook(workbook_path)
        ws = wb[SHEET] if SHEET in wb.sheetnames else None
        headers = [header for header, _, _ in COLUMNS]
        if ws is not None and [cell.value for cell in ws[1]] != headers:
            del wb[SHEET]  # written with an older set of columns; rebuilt from the JSON files
            ws = None
        ws = ws or _new_observation_sheet(wb)
    else:
        wb = Workbook()
        wb.remove(wb.active)
        ws = _new_observation_sheet(wb)

    existing_rows = {
        str(ws.cell(row, 1).value): row
        for row in range(2, ws.max_row + 1)
        if ws.cell(row, 1).value is not None
    }

    added = updated = 0
    for observation in observations:
        case_id = str(observation.get("case_id"))
        row = existing_rows.get(case_id)
        if row is None:
            row = ws.max_row + 1
            existing_rows[case_id] = row
            added += 1
        else:
            updated += 1
        _write_row(ws, row, observation)

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 2)}"
    _rebuild_summary(wb)

    try:
        wb.save(workbook_path)
    except PermissionError:
        raise PermissionError(
            f"Could not write {workbook_path} - close it in Excel and run again."
        ) from None
    return added, updated


def sync_observation_dir(observation_dir: Path, workbooks: list[Path]):
    """Write every <id>Observation.json in observation_dir to each of the workbooks."""
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(observation_dir.glob("*Observation.json"))
    ]
    for path in workbooks:
        added, updated = update_workbook(observations, path)
        print(f"Excel: {added} new row(s), {updated} refreshed -> {path}")

