import os
import tempfile
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


def run_ocr(pdf_file, prompt, temperature=1.0):
    return client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[
            pdf_file,
            prompt,
        ],
        config={
            "temperature": temperature,
        },
    )


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
    language = os.getenv("TESSERACT_LANG", "eng")
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