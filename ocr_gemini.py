"""OCR every grievance in one folder with Gemini, and nothing else (no observation, no Excel).

Reads the documents where they are (nothing is copied), from the folder you pick:
    <folder>\\<id>\\ATR\\..., Citizen\\...
Writes the text, one file per subfolder:
    Atr and Citizen_Out\\<id>\\ATR.txt, Citizen.txt     (the same folder run_folder.py --ocr gemini reads)

Text already there is kept, so running it again continues where the last run stopped.
Needs GEMINI_API_KEY in the .env file.

Usage:
    python ocr_gemini.py                  # pick a folder
    python ocr_gemini.py --source "D:\\PGRS\\PrajaSevaDocuments\\Atr and Citizen 2"
    python ocr_gemini.py --limit 50 --workers 5
    python ocr_gemini.py --model gemini-3.1-pro-preview
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app import ocr
from pipeline import find_cases, pick_folder
from run_folder import FAILED_FILE, TEXT_DIRS, failed_ids, note_failed, ocr_grievance


ENGINE = "gemini"
DEFAULT_MODEL = "gemini-3.8-flash"  # newest stable Gemini Flash (October 2026); reads PDFs and images


def main():
    parser = argparse.ArgumentParser(description="OCR every grievance in a folder with Gemini.")
    parser.add_argument("--source", type=Path, help="Folder of grievances. Omit to pick one in a folder picker.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default {DEFAULT_MODEL}).")
    parser.add_argument("--limit", type=int, help="Only this many grievances on this run.")
    parser.add_argument("--workers", type=int, default=3, help="Grievances OCR'd at the same time (default 3).")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry grievances whose OCR failed before.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    ocr.gemini_client()  # stop now if GEMINI_API_KEY is missing
    ocr.GEMINI_MODEL = args.model

    source = args.source or pick_folder()
    if source is None:
        raise SystemExit("No folder selected.")
    if not source.is_dir():
        raise SystemExit(f"Folder not found: {source}")
    text_dir = TEXT_DIRS[ENGINE]

    def is_done(case_dir: Path) -> bool:
        return all((text_dir / case_dir.name / f"{name}.txt").exists() for name in ocr.SUBFOLDERS)

    cases = find_cases(source)
    skipped = set() if args.retry_failed else failed_ids(ENGINE)
    todo = [p for p in cases if not is_done(p) and p.name not in skipped]
    todo = todo[: args.limit] if args.limit else todo
    print(f"Folder: {source}")
    print(f"{len(cases)} grievance(s), {len(cases) - len(todo)} already done or skipped; this run: {len(todo)}")
    if skipped:
        print(f"Skipping {len(skipped)} grievance(s) whose OCR failed before (see {FAILED_FILE}; --retry-failed to retry)")
    print(f"Model: {args.model}, text in: {text_dir}")

    finished, failed, started = 0, [], time.time()

    def logger(case_id):
        return lambda message: print(f"{case_id}: {message}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(ocr_grievance, p, ENGINE, logger(p.name)): p.name for p in todo}
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                job.result()
            except Exception as exc:
                failed.append(case_id)
                note_failed(ENGINE, case_id, exc)
                print(f"{case_id}: OCR FAILED, skipped on later runs: {exc}", flush=True)
            finished += 1
            elapsed = time.time() - started
            print(
                f"--- {finished}/{len(todo)} done, {elapsed / 60:.1f} min, "
                f"about {elapsed / finished * (len(todo) - finished) / 60:.0f} min left ---",
                flush=True,
            )

    print(f"\nDone in {(time.time() - started) / 60:.0f} min. Failed: {', '.join(failed) or 'none'}")
    print(f"OCR cost this run: ${sum(call['cost_usd'] for call in ocr.usage_log):.4f}")
    print(f"Text: {text_dir}")


if __name__ == "__main__":
    main()
