"""Upload one grievance folder, OCR its ATR and Citizen documents, and get the observation.

The folder must look like the ones in PGRS:
    <grievance_id>/ATR/<pdf or image files>
    <grievance_id>/Citizen/<pdf or image files>

Writes the same files as the command-line tools:
    PGRS/<grievance_id>/ATR|Citizen/...        (the uploaded files)
    PGRS_Output/<grievance_id>/ATR.txt, Citizen.txt
    Folder and Txt/PGRS/<grievance_id>/ATR|Citizen/...  and  .../Extracted Text/ATR.txt, Citizen.txt
    Observation/<grievance_id>Verdict.json (+ Verdicts.xlsx), also copied to Folder and Txt/Observation

Run:
    streamlit run streamlit_app.py
"""

import json
import time
import urllib.error
from pathlib import Path, PurePosixPath

import streamlit as st

from app.batch_ocr import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR, IMAGE_EXTENSIONS, PDF_EXTENSIONS, SUBFOLDERS
from app.compare_atr import DEFAULT_VERDICT_DIR, OLLAMA_MODEL, OLLAMA_URL, PROJECT_ROOT, check_case, save_verdict


# Copy of each grievance folder, with its extracted text alongside the documents.
SHARE_DIR = PROJECT_ROOT / "Folder and Txt" / "PGRS"
SHARE_OBSERVATION_DIR = PROJECT_ROOT / "Folder and Txt" / "Observation"
EXTRACTED_TEXT_FOLDER = "Extracted Text"
BACKENDS = {
    "Gemini (accurate, paid)": "gemini",
    f"Qwen {OLLAMA_MODEL} (local, free, slower)": "ollama",
}
SUPPORTED = PDF_EXTENSIONS | IMAGE_EXTENSIONS


def split_upload(uploads) -> tuple[str | None, dict, list[str]]:
    """Group uploaded files into ATR / Citizen by their folder path.

    Returns (grievance id, {"ATR": [...], "Citizen": [...]}, problems).
    """
    groups = {subfolder: [] for subfolder in SUBFOLDERS}
    case_ids, ignored = set(), []
    for upload in uploads:
        parts = PurePosixPath(upload.name.replace("\\", "/")).parts
        # The subfolder is matched by name, so <id>/ATR/x.pdf and <id>/atr/scans/x.pdf both work.
        index = next(
            (i for i, part in enumerate(parts[:-1]) if part.casefold() in {s.casefold() for s in SUBFOLDERS}),
            None,
        )
        if index is None or Path(parts[-1]).suffix.lower() not in SUPPORTED:
            ignored.append(upload.name)
            continue
        subfolder = next(s for s in SUBFOLDERS if s.casefold() == parts[index].casefold())
        groups[subfolder].append(upload)
        if index > 0:
            case_ids.add(parts[index - 1])

    problems = [f"No PDF or image found in the {s} folder." for s, files in groups.items() if not files]
    if len(case_ids) > 1:
        problems.append(f"Upload one grievance folder at a time (found {', '.join(sorted(case_ids))}).")
    if ignored:
        problems.append(f"Ignored {len(ignored)} file(s) outside ATR/Citizen or of an unsupported type.")
    return (next(iter(case_ids)) if len(case_ids) == 1 else None), groups, problems


def save_uploads(case_id: str, subfolder: str, uploads) -> list[Path]:
    """Save to PGRS (what the OCR reads) and to Folder and Txt/PGRS; returns the PGRS paths."""
    names = [PurePosixPath(upload.name.replace("\\", "/")).name for upload in uploads]
    for root in (SHARE_DIR, DEFAULT_INPUT_DIR):
        folder = root / case_id / subfolder
        folder.mkdir(parents=True, exist_ok=True)
        for name, upload in zip(names, uploads):
            (folder / name).write_bytes(upload.getvalue())
    return sorted(DEFAULT_INPUT_DIR / case_id / subfolder / name for name in names)


def run_case(case_id: str, groups: dict, backend: str, force_ocr: bool) -> dict:
    from app import ocr  # needs GEMINI_API_KEY; imported here so the page still loads without it
    from app.batch_ocr import ocr_files

    texts, ocr_cost = {}, 0.0
    with st.status("Working...", expanded=True) as status:
        for subfolder in SUBFOLDERS:
            files = save_uploads(case_id, subfolder, groups[subfolder])
            text_path = DEFAULT_OUTPUT_DIR / case_id / f"{subfolder}.txt"
            if text_path.exists() and not force_ocr:
                st.write(f"{subfolder}: using the text already extracted earlier.")
                texts[subfolder] = text_path.read_text(encoding="utf-8")
            else:
                st.write(f"{subfolder}: running OCR on {len(files)} file(s)...")
                started = time.time()
                ocr.usage_log.clear()
                texts[subfolder] = ocr_files(files)
                ocr_cost += sum(call["cost_usd"] for call in ocr.usage_log)
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(texts[subfolder], encoding="utf-8")
                st.write(f"{subfolder}: done in {time.time() - started:.0f}s.")

            extracted = SHARE_DIR / case_id / EXTRACTED_TEXT_FOLDER
            extracted.mkdir(parents=True, exist_ok=True)
            (extracted / f"{subfolder}.txt").write_text(texts[subfolder], encoding="utf-8")

        st.write("Comparing the ATR with the citizen's grievance...")
        started = time.time()
        result = check_case(case_id, texts["Citizen"], texts["ATR"], backend)
        saved, excel_note = save_verdict(result, DEFAULT_VERDICT_DIR)
        save_verdict(result, SHARE_OBSERVATION_DIR)
        st.write(f"Observation ready in {time.time() - started:.0f}s ({excel_note}).")
        status.update(label="Done", state="complete", expanded=False)

    return {"case_id": case_id, "texts": texts, "observation": saved, "ocr_cost": ocr_cost}


def show_result(run: dict):
    observation = run["observation"]
    st.subheader(f"Final observation: grievance {run['case_id']}")
    if observation["is_resolved"]:
        st.success("Resolved")
    else:
        st.error("Not resolved")

    st.markdown(f"**Formal remark:** {observation['formal_remark']}")
    st.markdown(f"**Reason:** {observation['reason']}")
    st.markdown("**ATR key sentence**")
    st.write(observation["atr_key_sentence"])
    st.markdown("**ATR key sentence (English)**")
    st.write(observation["atr_key_sentence_english"])

    st.download_button(
        "Download observation (JSON)",
        json.dumps(observation, ensure_ascii=False, indent=2),
        file_name=f"{run['case_id']}Verdict.json",
        mime="application/json",
    )
    if run["ocr_cost"]:
        st.caption(f"OCR cost for this upload: ${run['ocr_cost']:.4f}")
    st.caption(
        f"Saved to {SHARE_DIR / run['case_id'] / EXTRACTED_TEXT_FOLDER}, "
        f"{DEFAULT_OUTPUT_DIR / run['case_id']}, {DEFAULT_VERDICT_DIR} and {SHARE_OBSERVATION_DIR}"
    )

    st.subheader("Extracted text")
    for column, subfolder in zip(st.columns(len(SUBFOLDERS)), SUBFOLDERS):
        with column:
            st.markdown(f"**{subfolder}.txt**")
            st.text_area(subfolder, run["texts"][subfolder], height=400, label_visibility="collapsed")
            st.download_button(
                f"Download {subfolder}.txt",
                run["texts"][subfolder],
                file_name=f"{subfolder}.txt",
                key=f"download-{subfolder}",
            )


def main():
    st.set_page_config(page_title="PGRS Observation", layout="wide")
    st.title("PGRS grievance observation")
    st.write(
        "Upload one grievance folder (for example `365895`) that contains an **ATR** folder and a "
        "**Citizen** folder. Both are OCR'd, the text is saved as ATR.txt and Citizen.txt, and the "
        "ATR is checked against the citizen's request."
    )

    with st.sidebar:
        backend_label = st.radio("Model for the observation", list(BACKENDS))
        force_ocr = st.checkbox("Re-run OCR even if this grievance was extracted before")
        st.caption("OCR always uses Gemini. Qwen needs `ollama serve` running.")

    uploads = st.file_uploader(
        "Grievance folder",
        accept_multiple_files="directory",
        type=sorted(ext.lstrip(".") for ext in SUPPORTED),
    )
    if uploads:
        case_id, groups, problems = split_upload(uploads)
        case_id = st.text_input("Grievance ID", value=case_id or "").strip()
        for subfolder in SUBFOLDERS:
            names = ", ".join(PurePosixPath(u.name.replace("\\", "/")).name for u in groups[subfolder]) or "none"
            st.write(f"**{subfolder}:** {names}")
        for problem in problems:
            st.warning(problem)

        ready = case_id and all(groups[s] for s in SUBFOLDERS)
        if st.button("Extract text and find observation", type="primary", disabled=not ready):
            try:
                st.session_state["run"] = run_case(case_id, groups, BACKENDS[backend_label], force_ocr)
            except urllib.error.URLError as exc:
                st.error(f"Could not reach Ollama at {OLLAMA_URL}: {exc}. Is `ollama serve` running?")
            except Exception as exc:
                st.exception(exc)

    if "run" in st.session_state:
        show_result(st.session_state["run"])


main()
