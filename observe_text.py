"""Check grievances whose OCR is already done: text in, observation Excel out (no OCR here).

Reads one folder of text files, one .txt per document:
    <folder>\\<id>\\ATR\\*.txt, Citizen\\*.txt
Several files on one side are joined, each headed by its file name (as app.ocr does).
Only grievances with text on both sides are checked; the others are listed at the start.

Writes the observations to the one Excel file (Source column: "<folder name> Observation"):
    Observation\\All Observations Merged.xlsx   (saved every --batch grievances, and at the end)
Grievances already in that Excel are skipped, so running it again continues where it stopped.

Usage:
    python observe_text.py                          # pick the text folder, Qwen3 14B greedy server
    python observe_text.py --source "D:\\some\\text folder" --limit 10
    python observe_text.py --batch 50               # save to Excel every 50 grievances (default 25)
    python observe_text.py --source ipgrs_ouput --model gemini
    python observe_text.py --source api_output --half 1   # in a 2nd terminal --half 2: even / odd case IDs,
                                                          # so two terminals never check the same grievance
"""

import argparse
import sys
import time
from pathlib import Path

from app.excel import MERGED_WORKBOOK, update_workbook
from app.observation import MODELS, check_case
from pipeline import pick_folder
from run_folder import done_ids

SIDES = ("ATR", "Citizen")


def side_text(case_dir: Path, side: str) -> str:
    """All text files of one side, joined; empty if there are none or they are blank."""
    files = sorted((case_dir / side).glob("*.txt")) if (case_dir / side).is_dir() else []
    texts = [(f, f.read_text(encoding="utf-8").strip()) for f in files]
    texts = [(f, t) for f, t in texts if t]
    if len(texts) == 1:
        return texts[0][1]
    return "\n\n".join(f"===== {f.stem} =====\n\n{t}" for f, t in texts)


def in_half(case_id: str, half: int) -> bool:
    """--half 1: even case IDs, --half 2: odd ones (decided by the ID alone, so restarts never overlap)."""
    last = case_id[-1:]
    odd = int(last) % 2 == 1 if last.isdigit() else sum(case_id.encode()) % 2 == 1
    return odd == (half == 2)


def save(observations: list, workbook: Path, source: str):
    """Write the pending observations, waiting while the Excel is open elsewhere."""
    if not observations:
        return
    while True:
        try:
            update_workbook(observations, workbook, source)
            break
        except PermissionError as exc:
            print(f"WARNING: {exc} (retrying in 30s)", flush=True)
            time.sleep(30)
    print(f"Saved {len(observations)} to {workbook.name}", flush=True)
    observations.clear()


def main():
    parser = argparse.ArgumentParser(description="Check grievances from already extracted text.")
    parser.add_argument("--source", type=Path, help="Text folder, e.g. ipgrs_ouput or api_output. Omit to pick one.")
    parser.add_argument("--limit", type=int, help="Only this many grievances on this run.")
    parser.add_argument("--model", choices=MODELS, default="qwen14b", help="qwen14b (default, Qwen3 14B greedy server), qwen3, gemma or gemini.")
    parser.add_argument("--batch", type=int, default=25, help="Save to Excel every this many grievances (default 25).")
    parser.add_argument("--half", type=int, choices=(1, 2), help="Only even (1) or odd (2) case IDs, to run two terminals without overlap.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    args.source = args.source or pick_folder("Select a text folder (e.g. ipgrs_ouput or api_output)")
    if args.source is None:
        raise SystemExit("No folder selected.")
    if not args.source.is_dir():
        raise SystemExit(f"Folder not found: {args.source}")
    workbook, source = MERGED_WORKBOOK, f"{args.source.name} Observation"
    done = done_ids(workbook)

    cases, incomplete = [], {}
    for case_dir in sorted(p for p in args.source.iterdir() if p.is_dir()):
        missing = [side for side in SIDES if not side_text(case_dir, side)]
        if missing:
            incomplete[case_dir.name] = missing
        elif case_dir.name not in done and (args.half is None or in_half(case_dir.name, args.half)):
            cases.append(case_dir)
    print(f"Folder: {args.source}")
    print(f"{len(done)} already in {workbook.name}; {len(incomplete)} skipped for missing ATR or Citizen text; "
          f"{len(cases)} to go")
    cases = cases[: args.limit] if args.limit else cases
    print(f"This run: {len(cases)} grievance(s) with {MODELS[args.model]}"
          + (f", {'even' if args.half == 1 else 'odd'} case IDs only (--half {args.half})" if args.half else ""), flush=True)

    failed, pending, started = [], [], time.time()
    try:
        for index, case_dir in enumerate(cases, start=1):
            case_id = case_dir.name
            check_started = time.time()
            try:
                observation = check_case(case_id, side_text(case_dir, "Citizen"), side_text(case_dir, "ATR"), args.model)
                pending.append(observation)
                print(f"[{index}/{len(cases)}] {case_id}: {'★' * observation['rating']} "
                      f"({time.time() - check_started:.0f}s)", flush=True)
            except Exception as exc:
                failed.append(case_id)
                print(f"[{index}/{len(cases)}] {case_id}: FAILED: {exc}", flush=True)
            if len(pending) >= args.batch:
                save(pending, workbook, source)
    finally:
        save(pending, workbook, source)

    print(f"\nDone in {(time.time() - started) / 60:.0f} min. Failed: {', '.join(failed) or 'none'}")
    print(f"Excel: {workbook}")


if __name__ == "__main__":
    main()
