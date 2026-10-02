"""OCR the ATR and Citizen documents with Gemini (Tesseract when Gemini blocks a page).

Input:
    PGRS/<grievance_id>/ATR/<pdf or image files>
    PGRS/<grievance_id>/Citizen/<pdf or image files>

Output (one text file per subfolder; several files are combined into it):
    PGRS_Output/<grievance_id>/ATR.txt, Citizen.txt
    Folder and Txt/PGRS/<grievance_id>/Extracted Text/ATR.txt, Citizen.txt
    PGRS_Output/summary.json   (status, tokens and cost per folder)

Usage:
    python -m app.ocr
    python -m app.ocr --case 379425
    python -m app.ocr --force
"""

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv
from google import genai
import fitz
from PIL import Image
from pypdf import PdfReader, PdfWriter
import pytesseract


load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set in the .env file.")

client = genai.Client(api_key=API_KEY)


OCR_PROMPT = """
Read the supplied document using visual OCR.

Return only the text that is visibly present in the document.

Do not summarize.
Do not explain.
Do not translate.
Do not correct spelling or grammar.
Do not invent or infer text.
Preserve the original language and writing system.
Preserve names, numbers, dates, punctuation, paragraphs and line breaks where possible.
Preserve tables and their structure where possible.
If text is genuinely unreadable, write [UNCLEAR].
Output only the OCR result.
"""


OCR_PROMPT_RETRY = """
Perform visual OCR on the supplied document page.

Extract all readable text visible on the page and return only that text in reading order.

Keep the original language, spelling, names, numbers, dates and punctuation.
Do not summarize, translate, explain, interpret or correct the text.
Do not invent missing text.
Preserve paragraphs, line breaks and table structure where possible.
If something is genuinely unreadable, write [UNCLEAR].

Output only the extracted text.
"""


GEMINI_MODEL = "gemini-3.6-flash"

# USD per 1M tokens. Thinking tokens are billed at the output rate.
INPUT_PRICE_PER_M = float(os.getenv("GEMINI_INPUT_PRICE_PER_M", "1.50"))
OUTPUT_PRICE_PER_M = float(os.getenv("GEMINI_OUTPUT_PRICE_PER_M", "7.50"))

# One entry per Gemini call; callers may clear it to measure a batch of calls.
usage_log = []


def record_usage(response) -> None:
    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) or 0
    output_tokens = getattr(usage, "candidates_token_count", None) or 0
    thinking_tokens = getattr(usage, "thoughts_token_count", None) or 0

    cost = (
        input_tokens * INPUT_PRICE_PER_M
        + (output_tokens + thinking_tokens) * OUTPUT_PRICE_PER_M
    ) / 1_000_000

    usage_log.append({
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "cost_usd": cost,
    })


def run_ocr(pdf_file, prompt, temperature=1.0):
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            pdf_file,
            prompt,
        ],
        config={
            "temperature": temperature,
        },
    )
    record_usage(response)
    return response


def is_recitation_error(result_or_error) -> bool:
    if "recitation" in str(result_or_error).casefold():
        return True

    for candidate in getattr(result_or_error, "candidates", None) or []:
        finish_reason = getattr(candidate, "finish_reason", None)
        reason_name = getattr(finish_reason, "name", finish_reason)
        if "recitation" in str(reason_name).casefold():
            return True

    return False


def run_tesseract_ocr(pdf_document, page_number: int) -> str:
    page = pdf_document.load_page(page_number)
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(2, 2),
        colorspace=fitz.csRGB,
        alpha=False,
    )
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    language = os.getenv("TESSERACT_LANG", "eng+kan")
    return pytesseract.image_to_string(image, lang=language)


def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    all_text = []
    chunk_size = 5

    with tempfile.TemporaryDirectory() as temp_dir, fitz.open(str(pdf_path)) as visual_pdf:
        temp_dir = Path(temp_dir)

        for start in range(0, total_pages, chunk_size):
            end = min(start + chunk_size, total_pages)

            chunk_path = temp_dir / f"chunk_{start + 1}_{end}.pdf"

            writer = PdfWriter()

            for page_number in range(start, end):
                writer.add_page(reader.pages[page_number])

            with open(chunk_path, "wb") as chunk_file:
                writer.write(chunk_file)

            pdf = client.files.upload(file=str(chunk_path))

            try:
                response = run_ocr(
                    pdf,
                    OCR_PROMPT,
                    temperature=1.0,
                )
                if is_recitation_error(response):
                    for page_number in range(start, end):
                        all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                    continue
                ocr_text = response.text
            except Exception as exc:
                if not is_recitation_error(exc):
                    raise
                for page_number in range(start, end):
                    all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                continue

            if ocr_text is not None:
                all_text.append(ocr_text)
                continue

            # Fall back to individual pages
            for page_number in range(start, end):
                actual_page = page_number + 1

                single_page_path = temp_dir / f"page_{actual_page}.pdf"

                single_writer = PdfWriter()
                single_writer.add_page(reader.pages[page_number])

                with open(single_page_path, "wb") as single_file:
                    single_writer.write(single_file)

                single_pdf = client.files.upload(
                    file=str(single_page_path)
                )

                try:
                    single_response = run_ocr(
                        single_pdf,
                        OCR_PROMPT,
                        temperature=1.0,
                    )
                    if is_recitation_error(single_response):
                        all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                        continue
                    single_text = single_response.text
                except Exception as exc:
                    if not is_recitation_error(exc):
                        raise
                    all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                    continue

                if single_text is not None:
                    all_text.append(single_text)
                    continue

                try:
                    retry_response = run_ocr(
                        single_pdf,
                        OCR_PROMPT_RETRY,
                        temperature=1.0,
                    )
                    if is_recitation_error(retry_response):
                        all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                        continue
                    retry_text = retry_response.text
                except Exception as exc:
                    if not is_recitation_error(exc):
                        raise
                    all_text.append(run_tesseract_ocr(visual_pdf, page_number))
                    continue

                if retry_text is not None:
                    all_text.append(retry_text)
                else:
                    all_text.append(
                        f"[OCR FAILED FOR PAGE {actual_page}]"
                    )

    return "\n\n".join(all_text)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "PGRS"
TEXT_DIR = PROJECT_ROOT / "PGRS_Output"
# Copy of each grievance folder with its extracted text next to the documents.
SHARE_DIR = PROJECT_ROOT / "Folder and Txt" / "PGRS"
EXTRACTED_TEXT_FOLDER = "Extracted Text"

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


def ocr_files(files) -> str:
    """OCR several files into one text, each headed by its file name when there are several."""
    texts = []
    for file_path in files:
        text = ocr_file(file_path)
        if len(files) > 1:
            text = f"===== {file_path.name} =====\n\n{text}"
        texts.append(text)
    return "\n\n".join(texts)


def save_text(case_id: str, subfolder: str, text: str) -> Path:
    """Write <subfolder>.txt to PGRS_Output and to Folder and Txt; returns the PGRS_Output path."""
    for folder in (SHARE_DIR / case_id / EXTRACTED_TEXT_FOLDER, TEXT_DIR / case_id):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{subfolder}.txt").write_text(text, encoding="utf-8")
    return TEXT_DIR / case_id / f"{subfolder}.txt"


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
    parser = argparse.ArgumentParser(description="OCR every ATR/Citizen folder in PGRS.")
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

    input_dir, output_dir = INPUT_DIR, TEXT_DIR

    if not input_dir.is_dir():
        raise SystemExit(f"Input folder not found: {input_dir}")

    jobs = list(find_jobs(input_dir, args.cases))
    print(f"Found {len(jobs)} ATR/Citizen folders in {input_dir}")

    print(
        f"Pricing: ${INPUT_PRICE_PER_M}/1M input, "
        f"${OUTPUT_PRICE_PER_M}/1M output+thinking tokens"
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
        usage_log.clear()

        try:
            save_text(case_id, subfolder, ocr_files(files))
            status, error = "done", None
            print(f"    done in {time.time() - started:.1f}s")
        except Exception as exc:
            status, error = "failed", str(exc)
            entry["output"] = None
            print(f"    FAILED: {exc}")
            traceback.print_exc()

        calls = list(usage_log)
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
                    "input": INPUT_PRICE_PER_M,
                    "output_and_thinking": OUTPUT_PRICE_PER_M,
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
