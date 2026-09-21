import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pypdf import PdfReader, PdfWriter


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


def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    all_text = []
    chunk_size = 5

    with tempfile.TemporaryDirectory() as temp_dir:
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

            response = run_ocr(
                pdf,
                OCR_PROMPT,
                temperature=1.0,
            )

            ocr_text = response.text

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

                single_response = run_ocr(
                    single_pdf,
                    OCR_PROMPT,
                    temperature=1.0,
                )

                single_text = single_response.text

                if single_text is not None:
                    all_text.append(single_text)
                    continue

                retry_response = run_ocr(
                    single_pdf,
                    OCR_PROMPT_RETRY,
                    temperature=1.0,
                )

                retry_text = retry_response.text

                if retry_text is not None:
                    all_text.append(retry_text)
                else:
                    all_text.append(
                        f"[OCR FAILED FOR PAGE {actual_page}]"
                    )

    return "\n\n".join(all_text)