"""Write observations to Excel: every script adds its rows to the one workbook, MERGED_WORKBOOK.

New grievances are appended as rows; a grievance that is already in the sheet
(e.g. re-checked with --force) has its row updated in place. Rows already in
the sheet are never removed, so the workbook keeps growing across runs.

Used by app.observation: each observation is written to the sheet as soon as it
is produced, and the whole observation folder is re-synced at the end of the run.

The Source column (after COLUMNS) says which run wrote each row, e.g.
"Atr and Citizen Observation_Mandar". A lock file next to the workbook keeps two
runs at the same time from overwriting each other's rows.
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


MERGED_WORKBOOK = Path(__file__).resolve().parent.parent / "Observation" / "All Observations Merged.xlsx"
SHEET = "Observations"
SUMMARY_SHEET = "Summary"

# (header, observation key, column width)
COLUMNS = [
    ("Case ID", "case_id", 12),
    ("Resolved?", "is_resolved", 11),
    ("Rating", "rating", 10),
    ("Scheme (Kannada)", "scheme", 30),
    ("Scheme (English)", "scheme_english", 30),
    ("Case Summary", "case_summary", 50),
    ("Final Action Taken", "final_action_taken", 50),
    ("ATR Key Sentence (Kannada)", "atr_key_sentence", 50),
    ("ATR Key Sentence (English)", "atr_key_sentence_english", 50),
]

SOURCE_HEADER, SOURCE_WIDTH = "Source", 30
LOCK_STALE_SECONDS = 600  # a lock older than this was left by a run that crashed

RESOLVED_COLOURS = {"Yes": "C6EFCE", "No": "FFC7CE"}
RATING_STARS = "★"
STAR_FONT = Font(color="F4B400", size=14)

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
    if key == "rating":
        # Observations saved before ratings existed: 3 if resolved, else 1.
        stars = value or (3 if observation.get("is_resolved") else 1)
        return RATING_STARS * stars
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
    rating_cell = ws.cell(row, 3)
    rating_cell.font = STAR_FONT
    rating_cell.alignment = Alignment(horizontal="center", vertical="top")


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


@contextmanager
def _locked(path: Path):
    """Only one process reads-and-saves the workbook at a time."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.5)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def update_workbook(observations, workbook_path: Path = MERGED_WORKBOOK, source: str | None = None,
                    only_new: bool = False) -> tuple[int, int]:
    """Append new observations / update existing ones (only_new: leave rows already there).

    source fills the Source column. Returns (added, updated).
    """
    with _locked(workbook_path):
        return _update_workbook(observations, workbook_path, source, only_new)


def _update_workbook(observations, workbook_path, source, only_new):
    headers = [header for header, _, _ in COLUMNS]
    if workbook_path.exists():
        wb = load_workbook(workbook_path)
        ws = wb[SHEET] if SHEET in wb.sheetnames else None
        if ws is not None and [cell.value for cell in ws[1]][: len(headers)] != headers:
            del wb[SHEET]  # written with an older set of columns; rebuilt from the JSON files
            ws = None
        ws = ws or _new_observation_sheet(wb)
    else:
        wb = Workbook()
        wb.remove(wb.active)
        ws = _new_observation_sheet(wb)

    source_col = len(COLUMNS) + 1
    has_source = ws.cell(1, source_col).value == SOURCE_HEADER
    if source and not has_source:
        _style_header(ws.cell(1, source_col, SOURCE_HEADER))
        ws.column_dimensions[get_column_letter(source_col)].width = SOURCE_WIDTH
        has_source = True

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
        elif only_new:
            continue
        else:
            updated += 1
        _write_row(ws, row, observation)
        if has_source and source:
            cell = ws.cell(row, source_col, source)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    last_col = source_col if has_source else len(COLUMNS)
    ws.auto_filter.ref = f"A1:{get_column_letter(last_col)}{max(ws.max_row, 2)}"
    _rebuild_summary(wb)

    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb.save(workbook_path)
    except PermissionError:
        raise PermissionError(
            f"Could not write {workbook_path} - close it in Excel and run again."
        ) from None
    return added, updated


def sync_observation_dir(observation_dir: Path, workbook: Path = MERGED_WORKBOOK, source: str | None = None):
    """Add every <id>Observation.json in observation_dir that is not in the workbook yet."""
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(observation_dir.glob("*Observation.json"))
    ]
    added, _ = update_workbook(observations, workbook, source, only_new=True)
    print(f"Excel: {added} new row(s) -> {workbook}")
