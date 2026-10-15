"""One pipeline: pick a folder, OCR its documents, then find the observation.

The folder can be one grievance or a folder of grievances:
    <grievance_id>/ATR/<pdf or image files>
    <grievance_id>/Citizen/<pdf or image files>

For each grievance:
    1. copies the files to PGRS/<id>/ and Folder and Txt/PGRS/<id>/
    2. OCR (the hosted OCR server; --ocr indicapi, indic or gemini for another engine) -> PGRS_Output/<id>/ATR.txt, Citizen.txt
    3. observation (Gemma) -> Observation/<Model>/<id>Observation.json, Observations_<Model>.xlsx

Already extracted text and existing observations are reused unless --force is given.

Usage:
    python pipeline.py                     # opens a folder picker
    python pipeline.py "D:/Scans/379609"   # one grievance
    python pipeline.py "D:/Scans"          # every grievance inside
    python pipeline.py --model gemini --force
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

from app import ocr
from app.observation import MODELS, check_case, finish_run, observation_dirs, save_observation


SUPPORTED = ocr.PDF_EXTENSIONS | ocr.IMAGE_EXTENSIONS


def pick_folder(title: str = "Select a grievance folder (or a folder of grievances)") -> Path | None:
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    chosen = filedialog.askdirectory(title=title)
    root.destroy()
    return Path(chosen) if chosen else None


def document_folders(case_dir: Path) -> dict[str, Path]:
    """{"ATR": path, "Citizen": path}, matching the subfolder names in any case."""
    folders = {}
    for child in case_dir.iterdir():
        for subfolder in ocr.SUBFOLDERS:
            if child.is_dir() and child.name.casefold() == subfolder.casefold():
                folders[subfolder] = child
    return folders


def find_cases(folder: Path) -> list[Path]:
    """The folder itself if it is a grievance, otherwise every grievance folder inside it."""
    if document_folders(folder):
        return [folder]
    return sorted(p for p in folder.iterdir() if p.is_dir() and document_folders(p))


def copy_documents(case_id: str, subfolder: str, source: Path) -> list[Path]:
    """Copy the files to PGRS and Folder and Txt/PGRS; returns the PGRS copies, which get OCR'd."""
    files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
    for root in (ocr.INPUT_DIR, ocr.SHARE_DIR):
        target = root / case_id / subfolder
        target.mkdir(parents=True, exist_ok=True)
        for file in files:
            if (target / file.name).resolve() != file.resolve():
                shutil.copy2(file, target / file.name)
    return [ocr.INPUT_DIR / case_id / subfolder / file.name for file in files]


def ocr_case(case_dir: Path, force: bool, engine: str = "api", log=print) -> tuple[dict | None, bool]:
    """Copy and OCR one grievance. Returns ({"ATR": text, "Citizen": text} or None, whether OCR ran)."""
    case_id, ocr_ran = case_dir.name, False
    folders = document_folders(case_dir)
    missing = [s for s in ocr.SUBFOLDERS if s not in folders]
    if missing:
        log(f"    skipped: no {' or '.join(missing)} folder")
        return None, ocr_ran

    texts = {}
    for subfolder in ocr.SUBFOLDERS:
        files = copy_documents(case_id, subfolder, folders[subfolder])
        text_path = ocr.TEXT_DIR / case_id / f"{subfolder}.txt"
        if not files:
            log(f"    skipped: no PDF or image in {subfolder}")
            return None, ocr_ran
        if text_path.exists() and not force:
            log(f"    {subfolder}: using the text extracted earlier")
            texts[subfolder] = text_path.read_text(encoding="utf-8")
            continue
        log(f"    {subfolder}: OCR on {len(files)} file(s)...")
        started = time.time()
        texts[subfolder] = ocr.ocr_files(files, engine)
        ocr.save_text(case_id, subfolder, texts[subfolder])
        ocr_ran = True
        log(f"    {subfolder}: done in {time.time() - started:.0f}s")
    return texts, ocr_ran


def observe_case(case_id: str, texts: dict, model: str, redo: bool, log=print):
    """Check one grievance's OCR'd text, unless it already has an observation and redo is False."""
    if (observation_dirs(model)[0] / f"{case_id}Observation.json").exists() and not redo:
        log("    observation: already done")
        return
    log(f"    observation: checking with {MODELS[model]}...")
    started = time.time()
    observation = check_case(case_id, texts["Citizen"], texts["ATR"], model)
    note = save_observation(observation, model)
    status = "Resolved" if observation["is_resolved"] else "Not resolved"
    log(f"    observation: {status} ({time.time() - started:.0f}s), {note}")


def run_case(case_dir: Path, model: str, force: bool, engine: str = "api") -> float:
    """OCR and check one grievance; returns the OCR cost in USD."""
    calls_before = len(ocr.usage_log)
    texts, ocr_ran = ocr_case(case_dir, force, engine)
    if texts:
        observe_case(case_dir.name, texts, model, redo=force or ocr_ran)
    return sum(call["cost_usd"] for call in ocr.usage_log[calls_before:])


def main():
    parser = argparse.ArgumentParser(description="Pick a folder, OCR it, then find the observation.")
    parser.add_argument("folder", nargs="?", help="Grievance folder, or a folder of grievances. Omit to pick one.")
    parser.add_argument("--model", choices=MODELS, default="qwen3", help="qwen3 (default, Qwen3 14B on Ollama), qwen14b, gemma or gemini.")
    parser.add_argument("--ocr", choices=ocr.OCR_ENGINES, default="api", help="api (default, the OCR server at OCR_API_URL), indicapi (hosted IndicOCR server), indic (local Indic-OCR) or gemini.")
    parser.add_argument("--force", action="store_true", help="Redo OCR and the observation.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    folder = Path(args.folder) if args.folder else pick_folder()
    if folder is None:
        raise SystemExit("No folder selected.")
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")

    cases = find_cases(folder)
    if not cases:
        raise SystemExit(f"No grievance folder with ATR and Citizen subfolders found in {folder}")
    print(f"{len(cases)} grievance(s) in {folder}; observation with {MODELS[args.model]}")

    ocr_cost = 0.0
    for index, case_dir in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case_dir.name}")
        try:
            ocr_cost += run_case(case_dir, args.model, args.force, args.ocr)
        except Exception as exc:
            print(f"    FAILED: {exc}")

    finish_run(args.model)
    print(f"OCR cost this run: ${ocr_cost:.4f}")


if __name__ == "__main__":
    main()
