"""OCR with Indic-OCR (bodhan-ai/indic-ocr), the local model in indic-ocr/, instead of Gemini.

Free and offline, but it needs a GPU and, in the Python that runs the scripts:
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130   # your CUDA
    pip install -r indic-ocr/requirements.txt opencv-python fpdf2 pymupdf

The models are loaded once, on the first file, and reused for every file after that. Each
PDF or image becomes the same clean text that indic-ocr/batch_pgrs.py writes.
"""

import json
import sys
import tempfile
from pathlib import Path

INDIC_DIR = Path(__file__).resolve().parent.parent / "indic-ocr"

_state = {}


def _load():
    if not _state:
        sys.path.insert(0, str(INDIC_DIR))
        import batch_pgrs
        import main as indic_main

        args = indic_main.build_parser().parse_args(["--model-dir", str(INDIC_DIR)])
        args.keep_json, args.txt = True, False  # batch_pgrs builds the text from the JSON
        _state.update(main=indic_main, batch=batch_pgrs, args=args, parser=indic_main.setup(args))
    return _state


def ocr_file(file_path: Path) -> str:
    s = _load()
    try:
        with tempfile.TemporaryDirectory(prefix="indic_") as tmp:
            s["main"].run_document(s["parser"], file_path, Path(tmp), s["args"])
            result = json.loads((Path(tmp) / f"{file_path.stem}.json").read_text(encoding="utf-8"))
        return s["batch"].document_text(result, drop_unverified=False, page_headers="auto").strip()
    except Exception as exc:
        # Some documents make run_document fail AFTER the OCR, while it draws its annotated
        # PDF ("maximum recursion depth exceeded" inside fpdf2). Read the pages again
        # without that step: no handwriting re-read or checkboxes, but the text is there.
        print(f"    Indic-OCR full pipeline failed on {file_path.name} ({exc}); reading the pages directly",
              flush=True)
        return plain_text(file_path)


def plain_text(file_path: Path) -> str:
    s = _load()
    import preprocess

    texts = []
    with tempfile.TemporaryDirectory(prefix="indic_") as tmp:
        pages = s["main"].to_page_images(file_path, Path(tmp), s["args"].dpi, None)
        for page_no, image in pages:
            try:
                page = s["parser"].parse(str(image))
                preprocess.sanitize_page(page, False)
                text = s["batch"].page_to_text(page, drop_unverified=False)
            except Exception as exc:
                print(f"    Indic-OCR could not read page {page_no} of {file_path.name} ({exc})", flush=True)
                text = ""
            texts.append(f"===== Source page {page_no} =====\n{text}" if len(pages) > 1 else text)
    return "\n\n".join(t for t in texts if t.strip()).strip()
