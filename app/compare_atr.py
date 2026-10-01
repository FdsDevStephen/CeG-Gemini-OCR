"""Check whether each ATR (Action Taken Report) actually addresses the citizen's grievance.

Reads the OCR output written by app.batch_ocr:
    PGRS_Output/<grievance_id>/Citizen.txt
    PGRS_Output/<grievance_id>/ATR.txt

Sends both to a local Ollama model (qwen2.5:7b-instruct) and writes:
    Verdict/<grievance_id>Verdict.json
    Verdict/verdicts.json   (all grievances)
    Verdict/Verdicts.xlsx   (each verdict is added the moment it is produced)

Usage:
    python -m app.compare_atr
    python -m app.compare_atr --case 379609
    python -m app.compare_atr --force
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.verdict_excel import WORKBOOK_NAME, sync_verdict_dir, update_workbook


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "PGRS_Output"
DEFAULT_VERDICT_DIR = PROJECT_ROOT / "Verdict"
MODEL = "qwen2.5:7b-instruct"
OLLAMA_URL = "http://localhost:11434"

# verdict -> (plain meaning, is_resolved, formal remark). Kept in code so every
# verdict always carries the same explanation, whatever the model writes.
VERDICT_INFO = {
    "RESOLVED": (
        "The citizen's request was fulfilled as asked.",
        True,
        "The action taken is in conformity with the grievance raised and the matter stands resolved.",
    ),
    "PARTIALLY_RESOLVED": (
        "Only part of the request was fulfilled, or it was approved but the work is still pending.",
        False,
        "The grievance has been partly addressed; the remaining part requires follow-up.",
    ),
    "REDIRECTED": (
        "The office did nothing itself; it told the citizen to apply to, or forwarded the request to, "
        "another office (e.g. Gram Panchayat).",
        False,
        "The grievance has been redirected to another authority without resolution; the matter remains pending.",
    ),
    "REJECTED": (
        "The office refused the request (e.g. citizen found ineligible).",
        False,
        "The grievance has been rejected by the concerned office; the citizen may seek review if eligible.",
    ),
    "CLOSED_WITHOUT_ACTION": (
        "The office closed the request without doing anything and without directing the citizen elsewhere.",
        False,
        "The grievance has been closed without any action; the matter requires re-examination.",
    ),
    "WRONG_ACTION": (
        "The office did something different from what the citizen asked for "
        "(e.g. built a bridge instead of a road).",
        False,
        "The action taken does not correspond to the grievance raised; the matter requires re-examination.",
    ),
    "UNCLEAR": (
        "The documents are too unclear or incomplete to judge.",
        False,
        "The records are insufficient to assess the action taken; manual verification is required.",
    ),
}
VERDICTS = list(VERDICT_INFO)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "complaint_summary": {"type": "string"},
        "final_action": {"type": "string"},
        "verdict": {"type": "string", "enum": VERDICTS},
        "reason": {"type": "string"},
    },
    "required": ["complaint_summary", "final_action", "verdict", "reason"],
}

SYSTEM_PROMPT = """
You are a grievance redressal auditor for a government Public Grievance Redressal System (PGRS).

You receive two OCR'd documents, usually in Kannada:
1. CITIZEN: the grievance or request submitted by the citizen.
2. ATR: the Action Taken Report written by the government office in response.

Fill in, in English:
- complaint_summary: one or two sentences on what the citizen asked for.
- final_action: one plain sentence on what the office FINALLY did about it,
  e.g. "Told the citizen to apply to the Gram Panchayat; no site was allotted."
  or "Sanctioned a widow pension of Rs. 1,200 per month from October 2026."
- verdict: exactly one of the labels below.
- reason: why that label fits; also mention if the ATR seems to be about a different
  person, place or grievance number than the citizen's document.

Verdict labels:
- RESOLVED: the exact request was fulfilled or sanctioned
  (citizen asked for a road, and the road was built).
- PARTIALLY_RESOLVED: only part was fulfilled, or it was approved but work is pending.
- REDIRECTED: the office did not act itself; it told the citizen to apply elsewhere or
  forwarded the request to another office or Gram Panchayat. This is NOT partial resolution.
- REJECTED: the office refused the request, e.g. the citizen is not eligible.
- CLOSED_WITHOUT_ACTION: the request was closed/disposed (ಹಿಂಬರಹ / ವಿಲೇವಾರಿ) with no action
  and without telling the citizen where else to go.
- WRONG_ACTION: the office did something different from what was asked
  (citizen asked for a road, but a bridge was built).
- UNCLEAR: the text is too garbled or incomplete to judge.

Base your answer only on the two documents; do not invent facts.
""".strip()


def ask_ollama(citizen_text: str, atr_text: str) -> dict:
    user_prompt = (
        "=== CITIZEN DOCUMENT ===\n"
        f"{citizen_text.strip()}\n\n"
        "=== ATR (ACTION TAKEN REPORT) ===\n"
        f"{atr_text.strip()}\n\n"
        "Return the JSON verdict."
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": VERDICT_SCHEMA,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 16384},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read().decode("utf-8"))

    result = json.loads(body["message"]["content"])
    result["prompt_tokens"] = body.get("prompt_eval_count")
    result["output_tokens"] = body.get("eval_count")
    return result


def main():
    parser = argparse.ArgumentParser(description="Compare Citizen grievances with ATRs using Ollama.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="PGRS_Output folder.")
    parser.add_argument("--verdict-dir", type=Path, default=DEFAULT_VERDICT_DIR, help="Folder for <id>Verdict.json files.")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Only check this grievance folder (repeatable), e.g. --case 379609",
    )
    parser.add_argument("--force", action="store_true", help="Re-check even if the verdict already exists.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    output_dir = args.output.resolve()
    if not output_dir.is_dir():
        raise SystemExit(f"OCR output folder not found: {output_dir} (run app.batch_ocr first)")

    case_dirs = sorted(
        p for p in output_dir.iterdir()
        if p.is_dir() and (not args.cases or p.name in args.cases)
    )
    verdict_dir = args.verdict_dir.resolve()
    verdict_dir.mkdir(parents=True, exist_ok=True)
    print(f"Checking {len(case_dirs)} grievance(s) with {MODEL}")

    for index, case_dir in enumerate(case_dirs, start=1):
        label = f"[{index}/{len(case_dirs)}] {case_dir.name}"
        citizen_path = case_dir / "Citizen.txt"
        atr_path = case_dir / "ATR.txt"
        verdict_path = verdict_dir / f"{case_dir.name}Verdict.json"

        missing = [p.name for p in (citizen_path, atr_path) if not p.exists()]
        if missing:
            print(f"{label} -> skipped (missing {', '.join(missing)})")
            continue

        if verdict_path.exists() and not args.force:
            print(f"{label} -> skipped (already checked)")
            continue

        print(f"{label} -> comparing...", flush=True)
        started = time.time()

        try:
            result = ask_ollama(
                citizen_path.read_text(encoding="utf-8"),
                atr_path.read_text(encoding="utf-8"),
            )
        except urllib.error.URLError as exc:
            raise SystemExit(f"Could not reach Ollama at {OLLAMA_URL}: {exc}. Is `ollama serve` running?")
        except Exception as exc:
            print(f"    FAILED: {exc}")
            continue

        meaning, is_resolved, formal_remark = VERDICT_INFO[result["verdict"]]
        result = {
            "case_id": case_dir.name,
            "verdict": result["verdict"],
            "verdict_meaning": meaning,
            "is_resolved": is_resolved,
            "final_action": result["final_action"],
            "complaint_summary": result["complaint_summary"],
            "reason": result["reason"],
            "formal_remark": formal_remark,
            "model": MODEL,
            "prompt_tokens": result["prompt_tokens"],
            "output_tokens": result["output_tokens"],
        }
        verdict_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        try:
            update_workbook([result], verdict_dir / WORKBOOK_NAME)
            excel_note = "added to Excel"
        except PermissionError as exc:
            excel_note = f"Excel not updated yet: {exc}"

        print(f"    {result['verdict']} ({time.time() - started:.1f}s)")
        print(f"    Final action: {result['final_action']}")
        print(f"    {excel_note}")

    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(verdict_dir.glob("*Verdict.json"))
    ]

    summary_path = verdict_dir / "verdicts.json"
    summary_path.write_text(json.dumps(verdicts, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = {}
    for item in verdicts:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    print(f"\nVerdicts: {counts}")
    print(f"All verdicts written to {summary_path}")

    # Catch-up: also adds any verdicts that could not be written earlier
    # (e.g. because the workbook was open in Excel at the time).
    try:
        sync_verdict_dir(verdict_dir)
    except PermissionError as exc:
        print(f"WARNING: {exc}")


if __name__ == "__main__":
    main()
