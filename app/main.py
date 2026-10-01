import shutil
import tempfile
from pathlib import Path
import re

from fastapi import FastAPI, File, HTTPException, UploadFile

from app.ocr import extract_text_from_pdf


app = FastAPI(
    title="Gemini OCR API",
    description="OCR API using Gemini",
    version="1.0.0",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"


@app.get("/")
def root():
    return {
        "message": "Gemini OCR API is running"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.post("/ocr")
def perform_ocr(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported.",
        )

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "input.pdf"

            with open(pdf_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            text = extract_text_from_pdf(pdf_path)
            uploaded_name = Path(file.filename or "document.pdf").stem
            safe_stem = re.sub(r'[<>:"/\\|?*]+', "_", uploaded_name).strip(" .")
            safe_stem = safe_stem or "document"
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            text_path = OUTPUT_DIR / f"{safe_stem}.txt"
            text_path.write_text(text, encoding="utf-8")

            return {
                "filename": file.filename,
                "text": text,
                "text_file": str(text_path.relative_to(PROJECT_ROOT)),
            }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OCR processing failed: {str(exc)}",
        )
