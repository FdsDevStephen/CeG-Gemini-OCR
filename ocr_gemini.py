"""OCR a folder with Gemini, and nothing else (no observation, no Excel).

Reads the documents where they are (nothing is copied), from the folder you pick. Two kinds of folder:

  A folder of grievances:   <folder>\\<id>\\ATR\\..., Citizen\\...
      -> Atr and Citizen_Out\\<id>\\ATR.txt, Citizen.txt     (the same folder run_folder.py --ocr gemini reads)
  A folder of documents:    <folder>\\<any name>.pdf (or image), no grievance folders
      -> <folder name>_Out\\<document name>.txt              (one text file per document)

Text already there is kept, so running it again continues where the last run stopped.
Needs GEMINI_API_KEY in the .env file.

Usage:
    python ocr_gemini.py                  # pick a folder
    python ocr_gemini.py --source "D:\\PGRS\\PrajaSevaDocuments\\Atr and Citizen 2"
    python ocr_gemini.py --source smarana     # a folder of documents -> smarana_Out
    python ocr_gemini.py --limit 50 --workers 5
    python ocr_gemini.py --model gemini-3.1-pro-preview
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app import ocr
from pipeline import SUPPORTED, find_cases, pick_folder
from run_folder import FAILED_FILE, TEXT_DIRS, failed_ids, note_failed, ocr_grievance


ENGINE = "gemini"
DEFAULT_MODEL = "gemini-3.8-flash"  # newest stable Gemini Flash (October 2026); reads PDFs and images


def failed_names(text_dir: Path) -> set[str]:
    """Documents whose OCR failed on an earlier run (one "<name>\t<error>" per line)."""
    path = text_dir / FAILED_FILE
    if not path.exists():
        return set()
    return {line.split("\t", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def grievance_jobs(source: Path, retry_failed: bool):
    """(name, OCR function) for every grievance not OCR'd yet, and where its text goes."""
    text_dir = TEXT_DIRS[ENGINE]
    skipped = set() if retry_failed else failed_ids(ENGINE)
    jobs = []
    for case_dir in find_cases(source):
        if case_dir.name in skipped:
            continue
        if all((text_dir / case_dir.name / f"{name}.txt").exists() for name in ocr.SUBFOLDERS):
            continue
        log = lambda message, case_id=case_dir.name: print(f"{case_id}: {message}", flush=True)
        jobs.append((case_dir.name, lambda case_dir=case_dir, log=log: ocr_grievance(case_dir, ENGINE, log)))
    return jobs, text_dir, len(skipped), lambda name, exc: note_failed(ENGINE, name, exc)


def document_jobs(source: Path, retry_failed: bool):
    """(name, OCR function) for every document in the folder without a text file yet."""
    text_dir = ocr.PROJECT_ROOT / f"{source.name}_Out"
    skipped = set() if retry_failed else failed_names(text_dir)
    jobs = []
    for path in sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED):
        text_path = text_dir / f"{path.stem}.txt"
        if path.name in skipped or text_path.exists():
            continue

        def run(path=path, text_path=text_path):
            print(f"{path.name}: OCR started at {time.strftime('%H:%M:%S')}", flush=True)
            text = ocr.ocr_file(path)
            text_dir.mkdir(parents=True, exist_ok=True)
            text_path.write_text(text, encoding="utf-8")

        jobs.append((path.name, run))

    def note(name, exc):
        text_dir.mkdir(parents=True, exist_ok=True)
        reason = (str(exc).splitlines() or [type(exc).__name__])[0]
        with open(text_dir / FAILED_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{name}\t{reason}\n")

    return jobs, text_dir, len(skipped), note


def main():
    parser = argparse.ArgumentParser(description="OCR a folder of grievances or of documents with Gemini.")
    parser.add_argument("--source", type=Path, help="Folder to OCR. Omit to pick one in a folder picker.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default {DEFAULT_MODEL}).")
    parser.add_argument("--limit", type=int, help="Only this many grievances / documents on this run.")
    parser.add_argument("--workers", type=int, default=3, help="Grievances / documents OCR'd at the same time (default 3).")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry the ones whose OCR failed before.")
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

    if find_cases(source):
        kind, (todo, text_dir, skipped, note) = "grievance(s)", grievance_jobs(source, args.retry_failed)
    else:
        kind, (todo, text_dir, skipped, note) = "document(s)", document_jobs(source, args.retry_failed)
    todo = todo[: args.limit] if args.limit else todo
    print(f"Folder: {source}")
    print(f"This run: {len(todo)} {kind} not OCR'd yet")
    if skipped:
        print(f"Skipping {skipped} whose OCR failed before (see {FAILED_FILE}; --retry-failed to retry)")
    print(f"Model: {args.model}, text in: {text_dir}")

    finished, failed, started = 0, [], time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run): name for name, run in todo}
        for job in as_completed(jobs):
            name = jobs[job]
            try:
                job.result()
                print(f"{name}: OCR done", flush=True)
            except Exception as exc:
                failed.append(name)
                note(name, exc)
                print(f"{name}: OCR FAILED, skipped on later runs: {exc}", flush=True)
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
