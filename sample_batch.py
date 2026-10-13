"""Pick random grievances that have both ATR and Citizen documents, then OCR and check them.

OCR (Gemini, remote) and the observation (Gemma, local) run side by side: a few
grievances are OCR'd at once in the background while Gemma checks each one as soon
as its text is ready.

Each batch keeps its own folders, apart from PGRS/ and PGRS_Output/:
    PGRS 30/<id>/ATR/, Citizen/                       the picked documents
    PGRS30_Out/<id>/ATR.txt, Citizen.txt              the extracted text
    Folder and Txt/PGRS 30/<id>/ATR/, Citizen/, Extracted Text/ATR.txt, Citizen.txt
    Folder and Txt/PGRS 30/Observation1.xlsx
    Observation/Observation1/Observation1.xlsx, Observation1.json, <id>Observation.json, batch.json
    Folder and Txt/Observation/Observation1/       a copy of the folder above
A later batch of the same size gets "(Observation2)" added to its folder names.
The batch's observations are also added to Observation/All Observations Merged.xlsx (Source: Observation1).

Usage:
    python sample_batch.py                        # 30 random grievances, Gemma
    python sample_batch.py --count 10 --seed 7    # repeatable pick: same --seed, same grievances
    python sample_batch.py --resume Observation1  # finish a batch that was interrupted
"""

import argparse
import json
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app import ocr
from app.excel import MERGED_WORKBOOK, update_workbook
from app.observation import MODELS, OBSERVATION_ROOTS, check_case
from pipeline import SUPPORTED, document_folders, ocr_case


SOURCE_DIR = Path(r"D:\PGRS\PrajaSevaDocuments\Atr and Citizen")  # sorted by sort_documents.py
OCR_WORKERS = 3  # grievances OCR'd at the same time


def has_documents(case_dir: Path) -> bool:
    """True if both ATR and Citizen hold at least one PDF or image."""
    folders = document_folders(case_dir)
    return all(
        subfolder in folders
        and any(p.is_file() and p.suffix.lower() in SUPPORTED for p in folders[subfolder].rglob("*"))
        for subfolder in ocr.SUBFOLDERS
    )


def processed_ids() -> set[str]:
    """Grievances that already have extracted text, in PGRS_Output/ or an earlier batch."""
    text_dirs = [ocr.TEXT_DIR, *ocr.PROJECT_ROOT.glob("PGRS*_Out*")]
    return {p.name for d in text_dirs if d.is_dir() for p in d.iterdir() if p.is_dir()}


def pick_cases(source: Path, count: int, seed=None) -> list[Path]:
    """Shuffle every grievance folder and keep the first `count` that qualify.

    Without a seed the shuffle uses the operating system's cryptographic randomness,
    so every possible order of the folders is equally likely.
    """
    names = sorted(p.name for p in source.iterdir() if p.is_dir())
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    rng.shuffle(names)
    done, picked, checked = processed_ids(), [], 0
    for name in names:
        checked += 1
        if name not in done and has_documents(source / name):
            picked.append(source / name)
            if len(picked) == count:
                break
    print(f"Checked {checked} of {len(names)} folders to find {len(picked)} with ATR and Citizen")
    return picked


def new_batch(cases: list[Path], seed) -> Path:
    """Create Observation/ObservationN/batch.json with the picked grievances and the batch's folders."""
    number = 1
    while (OBSERVATION_ROOTS[0] / f"Observation{number}").exists():
        number += 1
    batch_dir = OBSERVATION_ROOTS[0] / f"Observation{number}"

    suffix = ""
    if (ocr.PROJECT_ROOT / f"PGRS {len(cases)}").exists():  # an earlier batch of the same size
        suffix = f" ({batch_dir.name})"
    batch = {
        "seed": seed,
        "source": str(cases[0].parent),
        "cases": [c.name for c in cases],
        "documents": f"PGRS {len(cases)}{suffix}",
        "text": f"PGRS{len(cases)}_Out{suffix}",
    }
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch.json").write_text(json.dumps(batch, indent=2), encoding="utf-8")
    return batch_dir


def observe(case_id: str, texts: dict, model: str, batch_dir: Path, log):
    """Check one grievance and save it to the batch's own folder and workbook only."""
    path = batch_dir / f"{case_id}Observation.json"
    if path.exists():
        log("observation: already done")
        return
    log(f"observation: checking with {MODELS[model]}...")
    started = time.time()
    observation = check_case(case_id, texts["Citizen"], texts["ATR"], model)
    path.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    status = "Resolved" if observation["is_resolved"] else "Not resolved"
    log(f"observation: {status} ({time.time() - started:.0f}s)")


def main():
    parser = argparse.ArgumentParser(description="OCR and check a random sample of grievances.")
    parser.add_argument("--source", type=Path, default=SOURCE_DIR, help=f"Default: {SOURCE_DIR}")
    parser.add_argument("--count", type=int, default=30, help="How many grievances (default 30).")
    parser.add_argument("--seed", type=int, help="Repeat an earlier random pick.")
    parser.add_argument("--model", choices=MODELS, default="gemma", help="gemma (default) or gemini.")
    parser.add_argument("--resume", metavar="BATCH", help="Finish an interrupted batch, e.g. Observation1.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if args.resume:
        batch_dir = OBSERVATION_ROOTS[0] / args.resume
        if not (batch_dir / "batch.json").exists():
            raise SystemExit(f"No batch.json in {batch_dir}")
    else:
        if not args.source.is_dir():
            raise SystemExit(f"Folder not found: {args.source}")
        cases = pick_cases(args.source, args.count, args.seed)
        if not cases:
            raise SystemExit("No grievance with both ATR and Citizen documents found.")
        batch_dir = new_batch(cases, args.seed)

    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    cases = [Path(batch["source"]) / case_id for case_id in batch["cases"]]
    # Point the OCR step at this batch's folders instead of PGRS/ and PGRS_Output/.
    ocr.INPUT_DIR = ocr.PROJECT_ROOT / batch["documents"]
    ocr.SHARE_DIR = ocr.SHARE_DIR.parent / batch["documents"]
    ocr.TEXT_DIR = ocr.PROJECT_ROOT / batch["text"]
    print(f"Batch {batch_dir.name}: {len(cases)} grievance(s), observation with {MODELS[args.model]}")
    print(f"Documents: {ocr.INPUT_DIR}\nText: {ocr.TEXT_DIR}\nShare: {ocr.SHARE_DIR}")

    def logger(case_id):
        return lambda message: print(f"{case_id}: {message.strip()}", flush=True)

    started, checked = time.time(), 0
    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as pool:
        jobs = {pool.submit(ocr_case, case_dir, False, logger(case_dir.name)): case_dir for case_dir in cases}
        for job in as_completed(jobs):  # Gemma takes each grievance as soon as its OCR is done
            case_id = jobs[job].name
            try:
                texts, _ = job.result()
                if texts:
                    observe(case_id, texts, args.model, batch_dir, logger(case_id))
            except Exception as exc:
                print(f"{case_id}: FAILED: {exc}", flush=True)
            checked += 1
            print(f"--- {checked}/{len(cases)} done, {(time.time() - started) / 60:.1f} min ---", flush=True)

    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for case_dir in cases
        if (path := batch_dir / f"{case_dir.name}Observation.json").exists()
    ]
    (batch_dir / f"{batch_dir.name}.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    workbook = batch_dir / f"{batch_dir.name}.xlsx"
    update_workbook(observations, workbook)
    update_workbook(observations, MERGED_WORKBOOK, source=batch_dir.name)
    shutil.copy2(workbook, ocr.SHARE_DIR / workbook.name)
    shutil.copytree(batch_dir, OBSERVATION_ROOTS[1] / batch_dir.name, dirs_exist_ok=True)

    resolved = sum(item["is_resolved"] for item in observations)
    print(
        f"\nDone in {(time.time() - started) / 60:.0f} min: {len(observations)} of {len(cases)} checked, "
        f"resolved {resolved}, not resolved {len(observations) - resolved}"
    )
    print(f"OCR cost: ${sum(call['cost_usd'] for call in ocr.usage_log):.4f}")
    print(f"Excel: {workbook}\nCopy: {ocr.SHARE_DIR / workbook.name}")


if __name__ == "__main__":
    main()
