"""OCR grievance folders with the OCR server (app/ocr_api.py), and nothing else (no observation, no Excel).

Reads the documents where they are (nothing is copied), from one or more folders:
    <folder>\\<id>\\ATR\\..., Citizen\\...
Writes one text file per document, all into one folder laid out like ipgrs_ouput:
    api_output\\<id>\\ATR\\<document name>.txt, Citizen\\<document name>.txt
so the LLM step reads it on its own:
    python observe_text.py --source api_output

Text already there is kept, so running it again continues where the last run stopped.
A grievance with a document the server cannot read goes in "OCR failed.txt" (in the output
folder) and is skipped on later runs (--retry-failed to retry). If the server itself is down,
the run stops instead, without marking anything as failed: start it again when it is back.

Usage:
    python ocr_api_folders.py                 # pick a folder
    python ocr_api_folders.py --source "C:\\Users\\HP\\Desktop\\Atr and Citizen 1 - Part 1" --source "...Part 2"
    python ocr_api_folders.py --source "..." --out "D:\\OCR text"   # another output folder
    python ocr_api_folders.py --limit 50 --workers 2
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from app import ocr, ocr_api
from pipeline import SUPPORTED, document_folders, find_cases, pick_folder

DEFAULT_OUT = ocr.PROJECT_ROOT / "api_output"
FAILED_FILE = "OCR failed.txt"  # in the output folder: "<id>\t<error>" per line


def server_up() -> bool:
    """The server answers a GET on its POST-only endpoint with 405 straight away when it is running."""
    try:
        return requests.get(ocr_api.OCR_API_URL, timeout=20).status_code < 500
    except requests.RequestException:
        return False


def documents(case_dir: Path, out: Path) -> list[tuple[Path, Path]]:
    """(document, its text file) for every document of the grievance."""
    pairs = []
    for side, folder in document_folders(case_dir).items():
        for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED):
            pairs.append((path, out / case_dir.name / side / f"{path.stem}.txt"))
    return pairs


def ocr_grievance(case_dir: Path, out: Path):
    for path, text_path in documents(case_dir, out):
        if text_path.exists():
            continue
        started = time.time()
        text = ocr_api.ocr_file(path)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        print(f"{case_dir.name}: {text_path.parent.name}\\{path.name} ({time.time() - started:.0f}s)", flush=True)


def failed_ids(out: Path) -> set[str]:
    path = out / FAILED_FILE
    if not path.exists():
        return set()
    return {line.split("\t", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def note_failed(out: Path, case_id: str, error: Exception) -> None:
    out.mkdir(parents=True, exist_ok=True)
    reason = (str(error).splitlines() or [type(error).__name__])[0]
    with open(out / FAILED_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"{case_id}\t{reason}\n")


def main():
    parser = argparse.ArgumentParser(description="OCR grievance folders with the OCR server into one text folder.")
    parser.add_argument("--source", type=Path, action="append", help="Folder of grievances (repeatable). Omit to pick one.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Folder for the text (default {DEFAULT_OUT.name}).")
    parser.add_argument("--limit", type=int, help="Only this many grievances on this run.")
    parser.add_argument("--workers", type=int, default=1, help="Grievances OCR'd at the same time (default 1).")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry grievances whose OCR failed before.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    sources = args.source or [pick_folder()]
    if sources == [None]:
        raise SystemExit("No folder selected.")
    for source in sources:
        if not source.is_dir():
            raise SystemExit(f"Folder not found: {source}")
    if not server_up():
        raise SystemExit(f"The OCR server at {ocr_api.OCR_API_URL} is not answering; try again later.")

    out = args.out
    skipped = set() if args.retry_failed else failed_ids(out)
    cases = [case_dir for source in sources for case_dir in find_cases(source)]
    todo = [
        p for p in cases
        if p.name not in skipped and not all(text_path.exists() for _, text_path in documents(p, out))
    ]
    for source in sources:
        print(f"Folder: {source}")
    print(f"{len(cases)} grievance(s), {len(cases) - len(todo)} already done or skipped, {len(todo)} to go")
    if skipped:
        print(f"Skipping {len(skipped)} grievance(s) whose OCR failed before (see {FAILED_FILE}; --retry-failed to retry)")
    todo = todo[: args.limit] if args.limit else todo
    print(f"This run: {len(todo)}, OCR server: {ocr_api.OCR_API_URL}, text in: {out}")

    finished, failed, started = 0, [], time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(ocr_grievance, p, out): p.name for p in todo}
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                job.result()
            except Exception as exc:
                if not server_up():
                    # Not this grievance's fault: leave it unmarked, so the next run tries it again.
                    for other in jobs:
                        other.cancel()
                    print(f"{case_id}: OCR FAILED and the server is not answering: {exc}", flush=True)
                    print("\nStopping. Start the script again when the OCR server is back; it continues from here.")
                    break
                failed.append(case_id)
                note_failed(out, case_id, exc)
                print(f"{case_id}: OCR FAILED, skipped on later runs: {exc}", flush=True)
            finished += 1
            elapsed = time.time() - started
            print(
                f"--- {finished}/{len(todo)} done, {elapsed / 60:.1f} min, "
                f"about {elapsed / finished * (len(todo) - finished) / 60:.0f} min left ---",
                flush=True,
            )

    print(f"\nFinished {finished} in {(time.time() - started) / 60:.0f} min. Failed: {', '.join(failed) or 'none'}")
    print(f"Text: {out}")
    print(f"Next: python observe_text.py --source \"{out}\"")


if __name__ == "__main__":
    main()
