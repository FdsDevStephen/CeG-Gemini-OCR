"""Check grievances whose OCR is already done: text in, observation Excel out (no OCR here).

Reads one folder of text files, one .txt per document:
    <folder>\\<id>\\ATR\\*.txt, Citizen\\*.txt
Several files on one side are joined, each headed by its file name (as app.ocr does).
Only grievances with text on both sides are checked; the others are listed at the start.

Writes the observations to:
    Observation\\<folder name> Observation.xlsx   (saved after every grievance)
Grievances already in that Excel are skipped, so running it again continues where it stopped.

Usage:
    python observe_text.py                          # ipgrs_ouput, Qwen3 14B greedy server
    python observe_text.py --source "D:\\some\\text folder" --limit 10
    python observe_text.py --model gemini
"""

import argparse
import sys
import time
from pathlib import Path

from app.excel import update_workbook
from app.observation import MODELS, OBSERVATION_ROOTS, check_case
from run_folder import done_ids

DEFAULT_SOURCE = Path(__file__).resolve().parent / "ipgrs_ouput"
SIDES = ("ATR", "Citizen")


def side_text(case_dir: Path, side: str) -> str:
    """All text files of one side, joined; empty if there are none or they are blank."""
    files = sorted((case_dir / side).glob("*.txt")) if (case_dir / side).is_dir() else []
    texts = [(f, f.read_text(encoding="utf-8").strip()) for f in files]
    texts = [(f, t) for f, t in texts if t]
    if len(texts) == 1:
        return texts[0][1]
    return "\n\n".join(f"===== {f.stem} =====\n\n{t}" for f, t in texts)


def main():
    parser = argparse.ArgumentParser(description="Check grievances from already extracted text.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help=f"Text folder (default: {DEFAULT_SOURCE.name}).")
    parser.add_argument("--limit", type=int, help="Only this many grievances on this run.")
    parser.add_argument("--model", choices=MODELS, default="qwen14b", help="qwen14b (default, Qwen3 14B greedy server), qwen3, gemma or gemini.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if not args.source.is_dir():
        raise SystemExit(f"Folder not found: {args.source}")
    workbook = OBSERVATION_ROOTS[0] / f"{args.source.name} Observation.xlsx"
    done = done_ids(workbook)

    cases, incomplete = [], {}
    for case_dir in sorted(p for p in args.source.iterdir() if p.is_dir()):
        missing = [side for side in SIDES if not side_text(case_dir, side)]
        if missing:
            incomplete[case_dir.name] = missing
        elif case_dir.name not in done:
            cases.append(case_dir)
    print(f"Folder: {args.source}")
    print(f"{len(done)} already in {workbook.name}; {len(incomplete)} skipped for missing ATR or Citizen text; "
          f"{len(cases)} to go")
    cases = cases[: args.limit] if args.limit else cases
    print(f"This run: {len(cases)} grievance(s) with {MODELS[args.model]}", flush=True)

    failed, started = [], time.time()
    for index, case_dir in enumerate(cases, start=1):
        case_id = case_dir.name
        check_started = time.time()
        try:
            observation = check_case(case_id, side_text(case_dir, "Citizen"), side_text(case_dir, "ATR"), args.model)
            while True:
                try:
                    update_workbook([observation], workbook)
                    break
                except PermissionError as exc:
                    print(f"WARNING: {exc} (retrying in 30s)", flush=True)
                    time.sleep(30)
            print(f"[{index}/{len(cases)}] {case_id}: {'★' * observation['rating']} "
                  f"({time.time() - check_started:.0f}s)", flush=True)
        except Exception as exc:
            failed.append(case_id)
            print(f"[{index}/{len(cases)}] {case_id}: FAILED: {exc}", flush=True)

    print(f"\nDone in {(time.time() - started) / 60:.0f} min. Failed: {', '.join(failed) or 'none'}")
    print(f"Excel: {workbook}")


if __name__ == "__main__":
    main()
