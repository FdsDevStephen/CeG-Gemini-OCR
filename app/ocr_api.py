"""OCR through the hosted OCR server instead of running a model here.

The server takes one PDF or image per request and returns its text:
    POST <OCR_API_URL>   multipart field "file"  ->  200 text/plain (UTF-8)

If the server instead queues the file (202 {"job_id": ...}), the text is polled from
<server>/result/<job_id> until it is ready.

The server addresses come from .env (see .env.example): OCR_API_URL for this server,
INDIC_OCR_API_URL for the IndicOCR one below.

The IndicOCR server (bodhan-ai/indic-ocr) answers in JSON instead, and its text needs cleaning:
    POST <INDIC_OCR_API_URL>   multipart field "file"  ->  {"pages": [{"page_number", "markdown"}]}
Its markdown has "▁" where spaces belong, HTML tables, and <img> placeholders for photos;
clean_markdown turns that into plain text. Write INDIC_OCR_API_URL without a trailing slash:
with one, the server redirects to 127.0.0.1.
"""

import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from pypdf import PdfReader

load_dotenv()

OCR_API_URL = os.getenv("OCR_API_URL")
INDIC_OCR_API_URL = os.getenv("INDIC_OCR_API_URL")
REQUEST_TIMEOUT = 600  # seconds; a long scanned PDF can take minutes
INDIC_REQUEST_TIMEOUT = 3600  # seconds; IndicOCR needs ~30-45 s per page and may wait in the server queue
INDIC_CHUNK_PAGES = int(os.getenv("INDIC_OCR_CHUNK_PAGES", "2"))  # pages per request for a PDF (0 = whole file in one request)
INDIC_PARALLEL = int(os.getenv("INDIC_OCR_PARALLEL", "1"))  # chunks sent at the same time (server has several workers; try 3)
POLL_INTERVAL = 10  # seconds between checks on a queued job
RETRY_WAITS = (10, 30, 60)  # seconds to wait before each retry when the server is down or busy


def _post(file_path: Path, url: str = OCR_API_URL, params: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    if not url:
        raise RuntimeError("The OCR server address is not set: add OCR_API_URL / INDIC_OCR_API_URL to .env (see .env.example)")
    for wait in (*RETRY_WAITS, None):
        timed_out = False
        try:
            with open(file_path, "rb") as handle:
                response = requests.post(url, files={"file": (file_path.name, handle)}, params=params, timeout=(30, timeout))
            if response.status_code < 500:
                return response
            error = f"HTTP {response.status_code}: {response.text[:100]}"
            timed_out = response.status_code == 504
        except requests.Timeout as exc:
            error, timed_out = str(exc), True
        except requests.RequestException as exc:
            error = str(exc)
        # A timeout means the document is too long for the server; retrying only wastes minutes.
        if wait is None or timed_out:
            raise RuntimeError(f"OCR server failed on {file_path.name}: {error}")
        print(f"    OCR server busy or unreachable ({error[:60]}); retrying in {wait}s", flush=True)
        time.sleep(wait)


def _wait_for_job(job_id: str, file_name: str) -> str:
    url = urljoin(OCR_API_URL, f"/result/{job_id}")
    while True:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            time.sleep(POLL_INTERVAL)
            continue
        if response.status_code == 200:
            response.encoding = "utf-8"
            return response.text
        if response.status_code != 202:
            raise RuntimeError(f"OCR server lost job {job_id} for {file_name}: HTTP {response.status_code}")
        time.sleep(POLL_INTERVAL)


def ocr_file(file_path: Path) -> str:
    response = _post(file_path)
    if response.status_code == 202:
        return _wait_for_job(response.json()["job_id"], file_path.name).strip()
    if response.status_code != 200:
        raise RuntimeError(f"OCR server rejected {file_path.name}: HTTP {response.status_code} {response.text[:200]}")
    response.encoding = "utf-8"
    return response.text.strip()


def clean_markdown(markdown: str) -> str:
    """IndicOCR markdown -> plain text: real spaces, one table row per line, no tags or photo labels."""
    text = markdown.replace("\u2581", " ")
    text = re.sub(r"<img[^>]*>", "", text)
    text = re.sub(r"<br\s*/?>", " / ", text)
    text = re.sub(r"</t[hd]>\s*<t[hd][^>]*>", " | ", text)  # cell boundary
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)  # remaining table tags
    text = html.unescape(text)
    lines = [re.sub(r"(\s*\|)+\s*", " | ", re.sub(r"[ \t]+", " ", line)).strip(" |") for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _indic_request(file_path: Path, page_range: str | None) -> list:
    """One IndicOCR call (whole file, or only ?pages=<range>) -> list of {"page_number", "markdown"}."""
    params = {"pages": page_range} if page_range else None
    response = _post(file_path, INDIC_OCR_API_URL, params=params, timeout=INDIC_REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"IndicOCR server rejected {file_path.name}: HTTP {response.status_code} {response.text[:200]}")
    return response.json()["pages"]


def _indic_pages(file_path: Path) -> list:
    """Long PDFs go in small page ranges: each request stays short, a failed range is retried on its
    own (not the whole file from page 1), and ranges can run in parallel on the server's workers."""
    total = None
    if file_path.suffix.lower() == ".pdf" and INDIC_CHUNK_PAGES > 0:
        try:
            total = len(PdfReader(str(file_path)).pages)
        except Exception:
            total = None  # unreadable here -> let the server decide, whole file in one request
    if not total or total <= INDIC_CHUNK_PAGES:
        return _indic_request(file_path, None)

    ranges = [f"{start}-{min(start + INDIC_CHUNK_PAGES - 1, total)}" for start in range(1, total + 1, INDIC_CHUNK_PAGES)]
    with ThreadPoolExecutor(max_workers=max(1, INDIC_PARALLEL)) as pool:
        chunks = list(pool.map(lambda r: _indic_request(file_path, r), ranges))
    return [page for chunk in chunks for page in chunk]


def ocr_file_indic(file_path: Path) -> str:
    pages = _indic_pages(file_path)
    texts = []
    for page in pages:
        text = clean_markdown(page["markdown"])
        if not text:
            text = f"[OCR FAILED FOR PAGE {page['page_number']}]"
        texts.append(f"===== Source page {page['page_number']} =====\n{text}" if len(pages) > 1 else text)
    return "\n\n".join(texts)
