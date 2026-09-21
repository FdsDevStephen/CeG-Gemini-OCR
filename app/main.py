import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from app.ocr import extract_text_from_pdf


app = FastAPI(
    title="Gemini OCR API",
    description="OCR API using Gemini",
    version="1.0.0",
)


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

            return {
                "filename": file.filename,
                "text": text,
            }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OCR processing failed: {str(exc)}",
        )