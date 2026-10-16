"""OCR and check every grievance in one folder, keeping only the text and one Excel file.

Reads the documents where they are (nothing is copied), from the folder you pick:
    <folder>\\<id>\\ATR\\..., Citizen\\...
Keeps the extracted text, so a re-check never runs OCR twice:
    Atr and Citizen_Out_API\\<id>\\ATR.txt, Citizen.txt   (each --ocr engine has its own folder)
Writes the observations only to the one Excel file:
    Observation\\All Observations Merged.xlsx

OCR (the hosted OCR server, see app/ocr_api.py; --ocr indicapi, indic or gemini for another engine) runs a few
grievances ahead in the background while the model checks each one as soon as its
text is ready. Grievances already in the Excel file are
skipped, so running it again continues where the last run stopped.

Usage:
    python run_folder.py                 # pick a folder; every grievance not yet in the Excel
    python run_folder.py --limit 100     # only the next 100
    python run_folder.py --source "D:\\PGRS\\PrajaSevaDocuments\\Atr and Citizen 2"   # no picker
    python run_folder.py --ocr gemini    # Gemini OCR instead of the OCR server
"""

import argparse
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from openpyxl import load_workbook

from app import ocr
from app.excel import MERGED_WORKBOOK, SHEET, _locked, update_workbook
from app.observation import MODELS, check_case
from pipeline import SUPPORTED, document_folders, find_cases, pick_folder


# Each OCR engine keeps its own text, so switching engines never reuses the other's text.
TEXT_DIRS = {
    "api": ocr.PROJECT_ROOT / "Atr and Citizen_Out_API",
    "indicapi": ocr.PROJECT_ROOT / "Atr and Citizen_Out_IndicAPI",
    "indic": ocr.PROJECT_ROOT / "Atr and Citizen_Out_Indic",
    "gemini": ocr.PROJECT_ROOT / "Atr and Citizen_Out",
}
# Every engine writes to MERGED_WORKBOOK; the Source column says which one checked a grievance
# (the names of the Excel files each engine used to have). A grievance already there is skipped.
SOURCE = "Atr and Citizen Observation"
SOURCES = {"indicapi": f"{SOURCE}_Mandar", "api": f"{SOURCE}_Ujwal"}
OCR_WORKERS = {"api": 1, "indicapi": 1, "indic": 1, "gemini": 3}  # grievances OCR'd at the same time (indicapi / api: one by one, ATR then Citizen)
OCR_AHEAD = 6     # at most this many OCR'd grievances wait for Gemma
FAILED_FILE = "OCR failed.txt"  # in TEXT_DIRS[engine]; these grievances are skipped unless --retry-failed
CHECK_FAILED_FILE = "Observation failed.txt"  # in TEXT_DIRS[engine]; only a record, these are retried on every run
SAVE_EVERY = 5    # write the Excel file after every 5 observations (and at the end); a stopped run redoes at most 4


def done_ids(workbook: Path) -> set[str]:
    if not workbook.exists():
        return set()
    # Under the workbook's lock: reading while another terminal saves it fails half way (EOFError).
    with _locked(workbook):
        ws = load_workbook(workbook, read_only=True)[SHEET]
        return {str(row[0]) for row in ws.iter_rows(min_row=2, max_col=1, values_only=True) if row[0]}


def failed_ids(engine: str) -> set[str]:
    """Grievances whose OCR failed on an earlier run with this engine (one "<id>\t<error>" per line)."""
    path = TEXT_DIRS[engine] / FAILED_FILE
    if not path.exists():
        return set()
    return {line.split("\t", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def note_failed(engine: str, case_id: str, error: Exception, file_name: str = FAILED_FILE) -> None:
    TEXT_DIRS[engine].mkdir(parents=True, exist_ok=True)
    reason = (str(error).splitlines() or [type(error).__name__])[0]
    with open(TEXT_DIRS[engine] / file_name, "a", encoding="utf-8") as handle:
        handle.write(f"{case_id}\t{reason}\n")


def ocr_grievance(case_dir: Path, engine: str, log) -> dict:
    """{"ATR": text, "Citizen": text}, reusing text extracted on an earlier run."""
    texts = {}
    for subfolder, folder in document_folders(case_dir).items():
        text_path = TEXT_DIRS[engine] / case_dir.name / f"{subfolder}.txt"
        if text_path.exists():
            texts[subfolder] = text_path.read_text(encoding="utf-8")
            continue
        files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
        log(f"{subfolder} OCR started ({len(files)} file(s)) at {time.strftime('%H:%M:%S')}")
        started = time.time()
        texts[subfolder] = ocr.ocr_files(files, engine)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(texts[subfolder], encoding="utf-8")
        log(f"{subfolder} OCR done ({len(files)} file(s), {time.time() - started:.0f}s)")
    return texts


def main():
    parser = argparse.ArgumentParser(description="OCR and check every grievance in a folder.")
    parser.add_argument("--source", type=Path, help="Folder of grievances. Omit to pick one in a folder picker.")
    parser.add_argument("--ocr", choices=ocr.OCR_ENGINES, default="api", help="api (default, the OCR server at OCR_API_URL), indicapi (hosted IndicOCR server), indic (local Indic-OCR) or gemini.")
    parser.add_argument("--limit", type=int, help="Only this many grievances on this run.")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry grievances whose OCR failed before.")
    parser.add_argument("--model", choices=MODELS, default="qwen14b", help="qwen14b (default, Qwen3 14B greedy server), qwen3 (Ollama), gemma or gemini.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    source = args.source or pick_folder()
    if source is None:
        raise SystemExit("No folder selected.")
    if not source.is_dir():
        raise SystemExit(f"Folder not found: {source}")
    print(f"Folder: {source}")
    workbook, source_name = MERGED_WORKBOOK, SOURCES.get(args.ocr, SOURCE)
    done = done_ids(workbook)
    skipped = set() if args.retry_failed else failed_ids(args.ocr)
    todo = [p for p in find_cases(source) if p.name not in done | skipped]
    print(f"{len(done)} grievance(s) already done (in {workbook.name}), {len(todo)} to go")
    if skipped:
        print(f"Skipping {len(skipped)} grievance(s) whose OCR failed before (see {FAILED_FILE}; --retry-failed to retry)")
    todo = todo[: args.limit] if args.limit else todo
    print(f"This run: {len(todo)} grievance(s), OCR with {args.ocr}, observation with {MODELS[args.model]}")

    pending, finished, failed, started = [], 0, [], time.time()

    def save():
        try:
            update_workbook(pending, workbook, source_name)
            pending.clear()
        except PermissionError as exc:
            print(f"WARNING: {exc} (will retry)", flush=True)

    def logger(case_id):
        return lambda message: print(f"{case_id}: {message}", flush=True)

    with ThreadPoolExecutor(max_workers=OCR_WORKERS[args.ocr]) as pool:
        queue, running = iter(todo), {}
        while True:
            # Keep OCR only a few grievances ahead of Gemma.
            while len(running) < OCR_AHEAD and (case_dir := next(queue, None)) is not None:
                running[pool.submit(ocr_grievance, case_dir, args.ocr, logger(case_dir.name))] = case_dir
            if not running:
                break
            ready, _ = wait(running, return_when=FIRST_COMPLETED)
            for job in ready:
                case_id = running.pop(job).name
                log = logger(case_id)
                try:
                    texts = job.result()
                except Exception as exc:
                    # OCR failed (e.g. the server times out on a long PDF): later runs skip it.
                    failed.append(case_id)
                    note_failed(args.ocr, case_id, exc)
                    log(f"OCR FAILED, skipped on later runs: {exc}")
                    texts = None
                if texts is not None:
                    try:
                        check_started = time.time()
                        observation = check_case(case_id, texts["Citizen"], texts["ATR"], args.model)
                        pending.append(observation)
                        log(f"{'★' * observation['rating']} ({time.time() - check_started:.0f}s)")
                    except Exception as exc:
                        failed.append(case_id)
                        note_failed(args.ocr, case_id, exc, CHECK_FAILED_FILE)
                        log(f"FAILED (see {CHECK_FAILED_FILE}): {exc}")
                finished += 1
                elapsed = time.time() - started
                print(
                    f"--- {finished}/{len(todo)} done, {elapsed / 60:.1f} min, "
                    f"about {elapsed / finished * (len(todo) - finished) / 60:.0f} min left ---",
                    flush=True,
                )
                if len(pending) >= SAVE_EVERY:
                    save()

    save()
    if pending:
        print(f"ERROR: {len(pending)} observation(s) could not be written; close {workbook} and run again.")
    print(f"\nDone in {(time.time() - started) / 60:.0f} min. Failed: {', '.join(failed) or 'none'}")
    print(f"OCR cost this run: ${sum(call['cost_usd'] for call in ocr.usage_log):.4f}")
    print(f"Excel: {workbook}")


if __name__ == "__main__":
    main()
