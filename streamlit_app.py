"""Upload one grievance folder, OCR its ATR and Citizen documents, and get the observation.

The folder must look like the ones in PGRS:
    <grievance_id>/ATR/<pdf or image files>
    <grievance_id>/Citizen/<pdf or image files>

Writes the same files as the command-line tools:
    PGRS/<grievance_id>/ and Folder and Txt/PGRS/<grievance_id>/   (the uploaded files)
    PGRS_Output/<grievance_id>/ and .../Extracted Text/           (ATR.txt, Citizen.txt)
    Observation/<Model>/ and Folder and Txt/Observation/<Model>/    (<id>Observation.json, Observations_<Model>.xlsx)

Run:
    streamlit run streamlit_app.py
"""

import json
import time
from pathlib import Path, PurePosixPath

import streamlit as st

from app import ocr
from app.observation import MODELS, check_case, observation_dirs, save_observation


SUPPORTED = ocr.PDF_EXTENSIONS | ocr.IMAGE_EXTENSIONS


def file_name(upload) -> str:
    return PurePosixPath(upload.name.replace("\\", "/")).name


def split_upload(uploads) -> tuple[str | None, dict, list[str]]:
    """Group uploaded files into ATR / Citizen by their folder path.

    Returns (grievance id, {"ATR": [...], "Citizen": [...]}, problems).
    """
    groups = {subfolder: [] for subfolder in ocr.SUBFOLDERS}
    case_ids, ignored = set(), []
    for upload in uploads:
        parts = PurePosixPath(upload.name.replace("\\", "/")).parts
        # The subfolder is matched by name, so <id>/ATR/x.pdf and <id>/atr/scans/x.pdf both work.
        index = next(
            (i for i, part in enumerate(parts[:-1]) if part.casefold() in {s.casefold() for s in ocr.SUBFOLDERS}),
            None,
        )
        if index is None or Path(parts[-1]).suffix.lower() not in SUPPORTED:
            ignored.append(upload.name)
            continue
        subfolder = next(s for s in ocr.SUBFOLDERS if s.casefold() == parts[index].casefold())
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
    for root in (ocr.SHARE_DIR, ocr.INPUT_DIR):
        folder = root / case_id / subfolder
        folder.mkdir(parents=True, exist_ok=True)
        for upload in uploads:
            (folder / file_name(upload)).write_bytes(upload.getvalue())
    return sorted(ocr.INPUT_DIR / case_id / subfolder / file_name(upload) for upload in uploads)


def run_case(case_id: str, groups: dict, model: str, force_ocr: bool) -> dict:
    texts, ocr_cost = {}, 0.0
    with st.status("Working...", expanded=True) as status:
        for subfolder in ocr.SUBFOLDERS:
            files = save_uploads(case_id, subfolder, groups[subfolder])
            text_path = ocr.TEXT_DIR / case_id / f"{subfolder}.txt"
            if text_path.exists() and not force_ocr:
                st.write(f"{subfolder}: using the text already extracted earlier.")
                texts[subfolder] = text_path.read_text(encoding="utf-8")
            else:
                st.write(f"{subfolder}: running OCR on {len(files)} file(s)...")
                started = time.time()
                ocr.usage_log.clear()
                texts[subfolder] = ocr.ocr_files(files)
                ocr_cost += sum(call["cost_usd"] for call in ocr.usage_log)
                st.write(f"{subfolder}: done in {time.time() - started:.0f}s.")
            ocr.save_text(case_id, subfolder, texts[subfolder])

        st.write(f"Checking the ATR against the citizen's request with {MODELS[model]}...")
        started = time.time()
        observation = check_case(case_id, texts["Citizen"], texts["ATR"], model)
        note = save_observation(observation, model)
        st.write(f"Observation ready in {time.time() - started:.0f}s ({note}).")
        status.update(label="Done", state="complete", expanded=False)

    return {"case_id": case_id, "model": model, "texts": texts, "observation": observation, "ocr_cost": ocr_cost}


def show_result(run: dict):
    observation = run["observation"]
    st.subheader(f"Final observation: grievance {run['case_id']}")
    if observation["is_resolved"]:
        st.success("Resolved")
    else:
        st.error("Not resolved")

    st.markdown(f"**Case summary:** {observation['case_summary']}")
    st.markdown(f"**Final action taken:** {observation['final_action_taken']}")
    st.markdown("**ATR key sentence (Kannada)**")
    st.write(observation["atr_key_sentence"])
    st.markdown("**ATR key sentence (English)**")
    st.write(observation["atr_key_sentence_english"])

    st.download_button(
        "Download observation (JSON)",
        json.dumps(observation, ensure_ascii=False, indent=2),
        file_name=f"{run['case_id']}Observation.json",
        mime="application/json",
    )
    if run["ocr_cost"]:
        st.caption(f"OCR cost for this upload: ${run['ocr_cost']:.4f}")
    st.caption("Saved to " + ", ".join(str(folder) for folder in observation_dirs(run["model"])))

    st.subheader("Extracted text")
    for column, subfolder in zip(st.columns(len(ocr.SUBFOLDERS)), ocr.SUBFOLDERS):
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
        model = st.radio("Model for the observation", list(MODELS), format_func=MODELS.get)
        force_ocr = st.checkbox("Re-run OCR even if this grievance was extracted before")
        st.caption("OCR always uses Gemini. Gemma needs Ollama running.")

    uploads = st.file_uploader(
        "Grievance folder",
        accept_multiple_files="directory",
        type=sorted(ext.lstrip(".") for ext in SUPPORTED),
    )
    if uploads:
        case_id, groups, problems = split_upload(uploads)
        case_id = st.text_input("Grievance ID", value=case_id or "").strip()
        for subfolder in ocr.SUBFOLDERS:
            st.write(f"**{subfolder}:** {', '.join(file_name(u) for u in groups[subfolder]) or 'none'}")
        for problem in problems:
            st.warning(problem)

        ready = case_id and all(groups[s] for s in ocr.SUBFOLDERS)
        if st.button("Extract text and find observation", type="primary", disabled=not ready):
            try:
                st.session_state["run"] = run_case(case_id, groups, model, force_ocr)
            except Exception as exc:
                st.exception(exc)

    if "run" in st.session_state:
        show_result(st.session_state["run"])


main()
