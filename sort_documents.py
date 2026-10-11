"""List which grievance folders have ATR and/or Citizen documents, and optionally sort them.

Scans every <grievance_id> folder in D:\\PGRS\\PrajaSevaDocuments and writes
D:\\PGRS\\PrajaSevaDocuments\\Folder List.xlsx: one row per folder with its category
and file counts.

    ATR and Citizen   both subfolders hold at least one PDF or image
    Only ATR          only the ATR subfolder does
    Only Citizen      only the Citizen subfolder does
    Neither           neither does

With --move, each folder is also moved (not copied: same drive, so instant and
without using extra space) into a category folder inside the same root folder:
    PrajaSevaDocuments\\Atr and Citizen\\<id>
    PrajaSevaDocuments\\Atr\\<id>
    PrajaSevaDocuments\\Citizen\\<id>
Folders with neither stay where they are. Running it again picks up folders added later.

Usage:
    python sort_documents.py           # only make the list
    python sort_documents.py --move    # make the list and move the folders
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from app.excel import HEADER_FILL, HEADER_FONT


SOURCE_DIR = Path(r"D:\PGRS\PrajaSevaDocuments")
LIST_PATH = SOURCE_DIR / "Folder List.xlsx"
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# category -> folder it is moved to with --move
TARGETS = {
    "ATR and Citizen": SOURCE_DIR / "Atr and Citizen",
    "Only ATR": SOURCE_DIR / "Atr",
    "Only Citizen": SOURCE_DIR / "Citizen",
}


def count_documents(folder: str) -> int:
    """PDFs and images anywhere under folder."""
    count = 0
    for root, _, files in os.walk(folder):
        count += sum(Path(name).suffix.lower() in SUPPORTED for name in files)
    return count


def classify(case_dir: os.DirEntry) -> tuple[str, int, int]:
    counts = {"atr": 0, "citizen": 0}
    for child in os.scandir(case_dir.path):
        name = child.name.casefold()
        if child.is_dir() and name in counts:
            counts[name] += count_documents(child.path)
    atr, citizen = counts["atr"], counts["citizen"]
    if atr and citizen:
        return "ATR and Citizen", atr, citizen
    if atr:
        return "Only ATR", atr, citizen
    if citizen:
        return "Only Citizen", atr, citizen
    return "Neither", atr, citizen


def write_list(rows: list[tuple], path: Path):
    wb = Workbook(write_only=True)
    summary, folders = wb.create_sheet("Summary"), wb.create_sheet("Folders")

    totals = {}
    for _, category, _, _ in rows:
        totals[category] = totals.get(category, 0) + 1
    summary.append(["Category", "Folders"])
    for category in (*TARGETS, "Neither"):
        summary.append([category, totals.get(category, 0)])
    summary.append(["Total", len(rows)])

    folders.append(["Grievance ID", "Category", "ATR files", "Citizen files"])
    for case_id, category, atr, citizen in rows:
        folders.append([int(case_id) if case_id.isdigit() else case_id, category, atr, citizen])
    wb.save(path)

    # Style the headers (not possible while writing in write-only mode).
    from openpyxl import load_workbook
    wb = load_workbook(path)
    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        ws.freeze_panes = "A2"
    wb["Summary"].column_dimensions["A"].width = 20
    wb["Folders"].column_dimensions["B"].width = 18
    wb["Summary"][f"A{wb['Summary'].max_row}"].font = Font(bold=True)
    wb.save(path)
    return totals


def main():
    parser = argparse.ArgumentParser(description="List and sort grievance folders by ATR / Citizen.")
    parser.add_argument("--move", action="store_true", help="Also move the folders into the category folders.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    category_folders = {target.name for target in TARGETS.values()}
    case_dirs = sorted(
        (e for e in os.scandir(SOURCE_DIR) if e.is_dir() and e.name not in category_folders),
        key=lambda e: e.name,
    )
    print(f"Scanning {len(case_dirs)} folders in {SOURCE_DIR}...")
    rows, started = [], time.time()
    for index, case_dir in enumerate(case_dirs, start=1):
        rows.append((case_dir.name, *classify(case_dir)))
        if index % 5000 == 0:
            print(f"  {index}/{len(case_dirs)} ({time.time() - started:.0f}s)", flush=True)

    totals = write_list(rows, LIST_PATH)
    print(f"\nList written to {LIST_PATH}")
    for category, count in sorted(totals.items(), key=lambda item: -item[1]):
        print(f"  {category}: {count}")

    if args.move:
        for target in TARGETS.values():
            target.mkdir(exist_ok=True)
        moved = 0
        for case_id, category, _, _ in rows:
            if category in TARGETS and not (TARGETS[category] / case_id).exists():
                shutil.move(str(SOURCE_DIR / case_id), str(TARGETS[category] / case_id))
                moved += 1
        print(f"\nMoved {moved} folders into: " + ", ".join(str(t) for t in TARGETS.values()))


if __name__ == "__main__":
    main()
