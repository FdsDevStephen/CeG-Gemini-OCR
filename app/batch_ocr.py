"""Run OCR over every PDF/image inside the PGRS folder.

Expected layout:
    PGRS/<grievance_id>/ATR/<files>
    PGRS/<grievance_id>/Citizen/<files>

Output (one text file per subfolder):
    PGRS_Output/<grievance_id>/ATR.txt
    PGRS_Output/<grievance_id>/Citizen.txt

If a subfolder holds several files, their text is combined into the one .txt.

Usage:
    python -m app.batch_ocr
    python -m app.batch_ocr --case 379425
    python -m app.batch_ocr --input PGRS --output PGRS_Output --force
"""

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PIL import Image

from app import ocr
from app.ocr import extract_text_from_pdf


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "PGRS"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "PGRS_Output"

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp"}
SUBFOLDERS = ("ATR", "Citizen")


def image_to_pdf(image_path: Path, pdf_path: Path) -> None:
    with Image.open(image_path) as image:
        frames = []
        for frame_index in range(getattr(image, "n_frames", 1)):
            image.seek(frame_index)
            frames.append(image.convert("RGB"))

    frames[0].save(
        pdf_path,
        "PDF",
        resolution=200.0,
        save_all=True,
        append_images=frames[1:],
    )


def ocr_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()

    if suffix in PDF_EXTENSIONS:
        return extract_text_from_pdf(file_path)

    with tempfile.TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "image.pdf"
        image_to_pdf(file_path, pdf_path)
        return extract_text_from_pdf(pdf_path)


def summarize_usage(calls):
    return {
        "api_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "thinking_tokens": sum(c["thinking_tokens"] for c in calls),
        "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
    }


def load_previous_summary(summary_path: Path) -> dict:
    """Return earlier results keyed by (case_id, type) so partial runs merge in."""
    if not summary_path.exists():
        return {}
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    folders = data.get("folders", []) if isinstance(data, dict) else data
    return {(item["case_id"], item["type"]): item for item in folders}


def find_jobs(input_dir: Path, case_ids=None):
    """Yield (case_id, subfolder, [files]) for every ATR/Citizen folder."""
    supported = PDF_EXTENSIONS | IMAGE_EXTENSIONS

    for case_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        if case_ids and case_dir.name not in case_ids:
            continue
        for subfolder in SUBFOLDERS:
            folder = case_dir / subfolder
            if not folder.is_dir():
                continue

            files = sorted(
                p for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in supported
            )
            if files:
                yield case_dir.name, subfolder, files


def main():
    parser = argparse.ArgumentParser(description="Batch OCR for the PGRS folder.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Only process this grievance folder (repeatable), e.g. --case 379425",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run OCR even if the output .txt already exists.",
    )
    args = parser.parse_args()

    # Kannada file names would otherwise crash printing on a cp1252 Windows console.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    input_dir = args.input.resolve()
    output_dir = args.output.resolve()

    if not input_dir.is_dir():
        raise SystemExit(f"Input folder not found: {input_dir}")

    jobs = list(find_jobs(input_dir, args.cases))
    print(f"Found {len(jobs)} ATR/Citizen folders in {input_dir}")

    print(
        f"Pricing: ${ocr.INPUT_PRICE_PER_M}/1M input, "
        f"${ocr.OUTPUT_PRICE_PER_M}/1M output+thinking tokens"
    )

    summary_path = output_dir / "summary.json"
    previous = load_previous_summary(summary_path)
    summary = []
    run_calls = []

    for index, (case_id, subfolder, files) in enumerate(jobs, start=1):
        text_path = output_dir / case_id / f"{subfolder}.txt"
        label = f"[{index}/{len(jobs)}] {case_id}/{subfolder}"
        sources = [str(f.relative_to(input_dir)) for f in files]
        entry = {
            "case_id": case_id,
            "type": subfolder,
            "sources": sources,
            "output": str(text_path.relative_to(output_dir)),
        }

        if text_path.exists() and not args.force:
            print(f"{label} -> skipped (already done)")
            summary.append(previous.get((case_id, subfolder), {**entry, "status": "skipped"}))
            continue

        print(f"{label} -> running OCR on {len(files)} file(s)...", flush=True)
        started = time.time()
        ocr.usage_log.clear()

        try:
            texts = []
            for file_path in files:
                text = ocr_file(file_path)
                if len(files) > 1:
                    text = f"===== {file_path.name} =====\n\n{text}"
                texts.append(text)

            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text("\n\n".join(texts), encoding="utf-8")
            status, error = "done", None
            print(f"    done in {time.time() - started:.1f}s")
        except Exception as exc:
            status, error = "failed", str(exc)
            entry["output"] = None
            print(f"    FAILED: {exc}")
            traceback.print_exc()

        calls = list(ocr.usage_log)
        run_calls.extend(calls)
        for call_number, call in enumerate(calls, start=1):
            print(
                f"    call {call_number}: {call['input_tokens']} in / "
                f"{call['output_tokens']} out / {call['thinking_tokens']} thinking "
                f"-> ${call['cost_usd']:.6f}"
            )
        usage = summarize_usage(calls)
        print(f"    folder cost: ${usage['cost_usd']:.6f} ({usage['api_calls']} call(s))")

        summary.append({
            **entry,
            "status": status,
            "error": error,
            "usage": usage,
            "api_calls": calls,
        })

    run_usage = summarize_usage(run_calls)
    merged = dict(previous)
    merged.update({(item["case_id"], item["type"]): item for item in summary})
    all_folders = [merged[key] for key in sorted(merged)]
    total_usage = summarize_usage(
        [call for item in all_folders for call in item.get("api_calls", [])]
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "pricing_usd_per_1m_tokens": {
                    "input": ocr.INPUT_PRICE_PER_M,
                    "output_and_thinking": ocr.OUTPUT_PRICE_PER_M,
                },
                "total_usage": total_usage,
                "folders": all_folders,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    counts = {}
    for item in summary:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(f"\nFinished: {counts}")
    for name, usage in (("This run", run_usage), ("All recorded runs", total_usage)):
        print(
            f"{name}: {usage['api_calls']} API call(s), "
            f"{usage['input_tokens']} input / {usage['output_tokens']} output / "
            f"{usage['thinking_tokens']} thinking tokens -> ${usage['cost_usd']:.4f}"
        )
    print(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()
