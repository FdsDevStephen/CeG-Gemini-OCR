"""Check whether each ATR (Action Taken Report) actually addresses the citizen's grievance.

Reads the OCR output written by app.batch_ocr:
    PGRS_Output/<grievance_id>/Citizen.txt
    PGRS_Output/<grievance_id>/ATR.txt

Sends both to an LLM (Gemini by default, or a local Ollama model) and writes:
    Observation/<grievance_id>Verdict.json
    Observation/verdicts.json   (all grievances)
    Observation/Verdicts.xlsx   (each verdict is added the moment it is produced)

Usage:
    python -m app.compare_atr
    python -m app.compare_atr --case 379609
    python -m app.compare_atr --force
    python -m app.compare_atr --backend ollama   # local qwen2.5:7b (weak at Kannada)
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.verdict_excel import WORKBOOK_NAME, sync_verdict_dir, update_workbook


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "PGRS_Output"
DEFAULT_VERDICT_DIR = PROJECT_ROOT / "Observation"
OLLAMA_MODEL = "qwen2.5:7b-instruct"
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
    "PENDING": (
        "Nothing has been sanctioned or done yet; the office says it will act later "
        "(e.g. once the government fixes a target or releases funds).",
        False,
        "No action has been taken on the grievance yet; the matter remains pending with the office.",
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

# Fields saved to <id>Verdict.json and the workbook. The model still fills in
# VERDICT_SCHEMA in full; the extra fields only help it reason.
OUTPUT_FIELDS = (
    "case_id", "is_resolved", "atr_key_sentence", "atr_key_sentence_english",
    "reason", "formal_remark",
)

# Field order matters: the model writes them in this order, so it quotes and
# translates the ATR's decisive sentence before it is allowed to judge.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "complaint_summary": {"type": "string"},
        "atr_key_sentence": {"type": "string"},
        "atr_key_sentence_english": {"type": "string"},
        "final_action": {"type": "string"},
        "same_applicant": {"type": "string", "enum": ["YES", "NO", "UNCLEAR"]},
        "verdict": {"type": "string", "enum": VERDICTS},
        "reason": {"type": "string"},
    },
    "required": [
        "complaint_summary", "atr_key_sentence", "atr_key_sentence_english",
        "final_action", "same_applicant", "verdict", "reason",
    ],
    "propertyOrdering": [
        "complaint_summary", "atr_key_sentence", "atr_key_sentence_english",
        "final_action", "same_applicant", "verdict", "reason",
    ],
}

# Options on the standard Praja Seva Andolana application form (Kannada keyword ->
# English), so the ticked item reaches the model in English as well.
FORM_OPTIONS = {
    "ವಿಧವಾ": "widow pension",
    "ಅಂಗವಿಕಲ": "disability pension",
    "ಸಂಧ್ಯಾ": "Sandhya Suraksha old-age pension",
    "ಪಿಂಚಣಿ": "other pension",
    "ತಿದ್ದುಪಡಿ": "Pahani (RTC land record) correction",
    "ಪೋಡಿ": "Podi (land survey / subdivision)",
    "ಖಾತೆ": "Khata transfer",
    "ವಸತಿ": "housing scheme: site / house allotment",
    "ಪಡಿತರ": "new ration card",
    "ಸಾರಿಗೆ": "transport / KSRTC bus",
    "ರಸ್ತೆ": "road development",
    "ನೀರು": "drinking water",
    "ಸ್ಮಶಾನ": "cemetery development",
    "ದೇವಸ್ಥಾನ": "temple development / renovation",
    "ಶಾಲೆ": "government school / anganwadi development",
    "ನರೇಗಾ": "MGNREGA (job scheme)",
    "ಪೊಲೀಸ್": "police department service",
    "ಉದ್ಯೋಗ": "private employment",
    "ಕೆರೆ": "lake filling / irrigation",
    "ಗೃಹ ಲಕ್ಷ್ಮಿ": "Gruha Lakshmi scheme",
    "ಗೃಹ ಜ್ಯೋತಿ": "Gruha Jyothi (free electricity) scheme",
    "ಶಕ್ತಿ": "Shakti free bus scheme",
    "ಅನ್ನ": "Anna Bhagya (ration rice) scheme",
    "ಯುವ ನಿಧಿ": "Yuva Nidhi scheme",
}
TICK_RE = re.compile(r"\[\s*[✓✔☑xX]\s*\]|[✓✔☑]")
EMPTY_BOX_RE = re.compile(r"\[\s*\]")
# Outcome phrases looked up in the ATR in code, so a small model is handed their
# English meaning instead of having to read the Kannada (e.g. a negated "sanctioned").
# \s* lets a phrase match when OCR broke the line in the middle of it.
OUTCOME_PHRASES = [
    (r"ಮರು\s*ಚಾಲ್ತಿ", "the pension/benefit HAS BEEN RESTARTED (done)"),
    (r"ಮಂಜೂರಾತಿ\s*ನೀಡಿರುವುದಿಲ್ಲ|ಮಂಜೂರು\s*ಮಾಡಿರುವುದಿಲ್ಲ", "it has NOT been sanctioned"),
    (r"ಮಂಜೂರು\s*ಮಾಡಲಾಗಿ(?:ದೆ|ರುತ್ತದೆ)|ಮಂಜೂರಾತಿ\s*ನೀಡಲಾಗಿ(?:ದೆ|ರುತ್ತದೆ)", "it HAS BEEN sanctioned (done)"),
    (r"ಗುರಿ\s*ನಿಗದಿಪಡಿಸಿದ\s*ನಂತರ", "only AFTER the government fixes a target (future)"),
    (r"ಕ್ರಮ\s*ವಹಿಸಲಾಗು?ವುದು|ಕ್ರಮ\s*ಕೈಗೊಳ್ಳಲಾಗು?ವುದು", "action WILL be taken later (nothing done yet)"),
    (r"ಮುಕ್ತಾಯಗೊಳಿಸಲಾಗ", "the complaint is CLOSED"),
    (r"ವಿಲೇವಾರಿ\s*(?:ಗೊಳಿಸಲಾಗಿದೆ|ಮಾಡಲಾಗಿದೆ)", "the request has been DISPOSED of (closed)"),
    (r"ಲಗತ್ತಿಸಿರುವುದಿಲ್ಲ", "the citizen did NOT attach the required documents"),
    (r"ಕರೆಯನ್ನು\s*ಸ್ವೀಕರಿಸಿರುವುದಿಲ್ಲ", "the citizen did NOT answer the office's phone call"),
    (r"ಸಂಬಂಧವಿರುವುದಿಲ್ಲ", "the applicant says they have NO connection to the application"),
    (r"ಅರ್ಹರಿರುವುದಿಲ್ಲ|ಅನರ್ಹ", "the citizen is NOT eligible"),
    # \S, not \w: Python's \w does not match Kannada vowel signs.
    (r"(?:ಪಂಚಾಯಿತಿ|ಕಛೇರಿ|ಇಲಾಖೆ)\S*\s*(?:ಕೋರಿಕೆ|ಅರ್ಜಿ|ಮನವಿ)\S*\s*ಸಲ್ಲಿಸ|ಸಲ್ಲಿಸುವಂತೆ\s*ಸೂಚಿಸ",
     "the citizen was TOLD TO APPLY to another office (e.g. the Gram Panchayat)"),
    (r"ಕಳುಹಿಸಲಾಗಿದೆ|ವರ್ಗಾಯಿಸಲಾಗಿದೆ|ರವಾನಿಸಲಾಗಿದೆ", "the request was FORWARDED to another office"),
    (r"ಪ್ರಗತಿಯಲ್ಲಿದೆ", "the work is IN PROGRESS"),
    (r"ಸ್ಥಗಿತಗೊಂಡಿರುವುದರಿಂದ", "\"because it had stopped\" (background only, NOT the outcome)"),
]
GRIEVANCE_NO_RE = re.compile(r"(?:ಸಂಖ್ಯೆ|#)\s*:?\s*(\d{6})(?!\d)")
OPTION_NUMBER_RE = re.compile(r"(\d+|[IVXL]+)\s*[.)]")  # "8. ..." or "IV. ..."
KANNADA_RE = re.compile(r"[ಀ-೿]")
# A real English sentence has at least one of these; romanised Kannada
# ("tamma pinchani ... maru chalti") has none.
ENGLISH_WORD_RE = re.compile(
    r"\b(?:the|a|an|is|are|was|were|has|have|had|been|be|will|not|no|to|of|and|for|your|their|his|her)\b",
    re.IGNORECASE,
)

TRANSLATE_PROMPT = """
You translate Kannada sentences from Karnataka government Action Taken Reports into English.
Translate faithfully and completely: keep who did what, what was or was not done, and any date.
Do not summarise, do not add facts, and write English only - no Kannada script.
Verb endings: ...ಲಾಗಿದೆ / ...ಲಾಗಿರುತ್ತದೆ = has been done; ...ಲಾಗುವುದು = will be done;
...ಇರುವುದಿಲ್ಲ = has not been done.
Words: ಗ್ರಾಮ ಸಭೆ = Grama Sabha (village assembly); ಗ್ರಾಮ ಪಂಚಾಯಿತಿ = Gram Panchayat;
ಕೋರಿಕೆ ಸಲ್ಲಿಸಲು = to submit a request; ತಿಳಿಯಪಡಿಸಿದೆ = you are informed; ನಿವೇಶನ = site / plot;
ಪಿಂಚಣಿ ಸೌಲಭ್ಯ = pension benefit; ಮರು ಚಾಲ್ತಿ = restarted.
""".strip()

SYSTEM_PROMPT = """
You are a grievance redressal auditor for a government Public Grievance Redressal System (PGRS).

You receive two OCR'd documents, usually in Kannada:
1. CITIZEN: the grievance or request submitted by the citizen. It is often a printed
   application form listing many schemes; ONLY the ticked (✓) item(s) and any attached
   letter are what the citizen actually asked for. Ignore the options that are not ticked.
2. ATR: the Action Taken Report written by the government office in response.

Read the ATR carefully. Its first sentences usually restate the request or the
background problem; the outcome is the sentence that says what WAS done, was NOT
done, or WILL be done. Watch the Kannada verb endings:
- ...ಲಾಗಿದೆ / ...ಲಾಗಿರುತ್ತದೆ / ...ಮಾಡಿದೆ = has been done (completed)
- ...ಲಾಗುವುದು = will be done (future; nothing done yet)
- ...ಇರುವುದಿಲ್ಲ / ...ನೀಡಿರುವುದಿಲ್ಲ = has NOT been done / NOT given

Common phrases:
- ಮರು ಚಾಲ್ತಿ ಮಾಡಲಾಗಿರುತ್ತದೆ = has been restarted / reactivated (done)
- ಸ್ಥಗಿತಗೊಂಡಿರುವುದರಿಂದ = because it had stopped (the earlier problem, not the outcome)
- ಮಂಜೂರು ಮಾಡಲಾಗಿದೆ / ಮಂಜೂರಾತಿ ನೀಡಲಾಗಿದೆ = has been sanctioned
- ಮಂಜೂರಾತಿ ನೀಡಿರುವುದಿಲ್ಲ = has NOT been sanctioned
- ಗುರಿ ನಿಗದಿಪಡಿಸಿದ ನಂತರ ... ಕ್ರಮವಹಿಸಲಾಗುವುದು = action will be taken after the
  government fixes a target (nothing sanctioned yet)
- ಅರ್ಹರಿರುವುದಿಲ್ಲ / ಅನರ್ಹ = not eligible
- ಅರ್ಜಿ ಸಲ್ಲಿಸಲು ಸೂಚಿಸಿದೆ = advised to apply (elsewhere)
- ಕಳುಹಿಸಲಾಗಿದೆ / ವರ್ಗಾಯಿಸಲಾಗಿದೆ = forwarded
- ಪ್ರಗತಿಯಲ್ಲಿದೆ = in progress
- ಹಿಂಬರಹ = endorsement / reply letter; ವಿಲೇವಾರಿ = disposed
- ಮನೆ = house; ನಿವೇಶನ / ಜಾಗ = site / plot; ವಸತಿ ಯೋಜನೆ = housing scheme

Fill in, in English:
- complaint_summary: one or two sentences on what the citizen asked for.
- atr_key_sentence: copy, word for word in the original language, the ATR sentence that
  states the outcome.
- atr_key_sentence_english: a faithful English translation of that sentence.
- final_action: one plain sentence on what the office FINALLY did, based on that sentence,
  e.g. "Restarted the citizen's suspended pension." or
  "No house was sanctioned; the application will be placed before the Grama Sabha once
  the government fixes a housing target."
- same_applicant: YES if the ATR is about the same person and place as the citizen's
  document, NO if the name, village or grievance number clearly differs, UNCLEAR otherwise.
- verdict: exactly one of the labels below.
- reason: why that label fits; mention any mismatch found for same_applicant.

Verdict labels:
- RESOLVED: the exact request was fulfilled, sanctioned or restored
  (citizen asked for a road and the road was built; a stopped pension was restarted).
- PARTIALLY_RESOLVED: only part was fulfilled, or it was sanctioned but the work is pending.
- PENDING: nothing has been sanctioned or done yet; the office says it will act later
  (e.g. after a target is fixed or funds are released). This is NOT partial resolution.
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


def ticked_items(citizen_text: str) -> list[str]:
    """Numbered form options the citizen ticked, each with its English meaning when known."""
    items = []
    for line in citizen_text.splitlines():
        # Table cells, and options stacked in one cell with <br>; drop the "(✓ ನಮೂದಿಸಿ)" hints.
        segments = [x.strip(" *") for x in re.split(r"\||<br>", re.sub(r"\([^)]*\)", "", line))]
        for i, segment in enumerate(segments):
            if not TICK_RE.search(segment):
                continue
            option = TICK_RE.sub("", segment).strip(" *") or next(
                (x for x in reversed(segments[:i]) if x and not EMPTY_BOX_RE.fullmatch(x)), ""
            )
            if not OPTION_NUMBER_RE.match(option):
                continue
            english = next((en for kn, en in FORM_OPTIONS.items() if kn in option), None)
            items.append(f"{option} ({english})" if english else option)
    return items


def outcome_phrases(atr_text: str) -> list[str]:
    """English meaning of each known outcome phrase found in the ATR, in reading order."""
    found = []
    for pattern, meaning in OUTCOME_PHRASES:
        match = re.search(pattern, atr_text)
        if match:
            found.append((match.start(), f"{' '.join(match.group().split())} = {meaning}"))
    return [text for _, text in sorted(found)]


def other_grievance_numbers(case_id: str, atr_text: str) -> list[str]:
    """Grievance numbers the ATR cites, if none of them is this case's number."""
    numbers = sorted(set(GRIEVANCE_NO_RE.findall(atr_text)))
    return [] if not numbers or case_id in numbers else numbers


def build_user_prompt(case_id: str, citizen_text: str, atr_text: str) -> str:
    ticked = ticked_items(citizen_text)
    notes = [
        "Ticked on the citizen's form: " + "; ".join(ticked)
        if ticked else "No ticked form option was detected; rely on the citizen's letter."
    ]
    phrases = outcome_phrases(atr_text)
    if phrases:
        notes.append("Outcome phrases found in the ATR (reliable translations):\n- " + "\n- ".join(phrases))
    others = other_grievance_numbers(case_id, atr_text)
    if others:
        notes.append(
            f"This case is grievance {case_id}, but the ATR cites grievance number(s) "
            f"{', '.join(others)}: it is probably a reply to a different grievance."
        )
    return (
        "=== CITIZEN DOCUMENT ===\n"
        f"{citizen_text.strip()}\n\n"
        "=== ATR (ACTION TAKEN REPORT) ===\n"
        f"{atr_text.strip()}\n\n"
        "=== CHECKS DONE IN CODE ===\n"
        + "\n".join(notes)
        + "\n\nReturn the JSON verdict."
    )


def ask_gemini(user_prompt: str) -> dict:
    from app import ocr  # needs GEMINI_API_KEY, so only imported for this backend

    response = ocr.client.models.generate_content(
        model=ocr.GEMINI_MODEL,
        contents=user_prompt,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "temperature": 0,
            "response_mime_type": "application/json",
            "response_schema": VERDICT_SCHEMA,
        },
    )
    ocr.record_usage(response)
    usage = ocr.usage_log[-1]

    result = json.loads(response.text)
    result["prompt_tokens"] = usage["input_tokens"]
    result["output_tokens"] = usage["output_tokens"] + usage["thinking_tokens"]
    return result


def ollama_chat(system: str, user: str, schema: dict, num_ctx: int) -> tuple[dict, dict]:
    """Returns (parsed JSON reply, raw response body)."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.loads(response.read().decode("utf-8"))
    return json.loads(body["message"]["content"]), body


def ask_ollama(user_prompt: str) -> dict:
    schema = {k: v for k, v in VERDICT_SCHEMA.items() if k != "propertyOrdering"}
    result, body = ollama_chat(SYSTEM_PROMPT, user_prompt, schema, num_ctx=16384)
    result["prompt_tokens"] = body.get("prompt_eval_count")
    result["output_tokens"] = body.get("eval_count")

    # qwen2.5:7b sometimes copies (or romanises) the Kannada instead of translating
    # it; translate that one sentence on its own. Only as a fallback: on sentences
    # it already got right, the stand-alone translation tends to invent details.
    english = result["atr_key_sentence_english"]
    if KANNADA_RE.search(english) or not ENGLISH_WORD_RE.search(english):
        hints = outcome_phrases(result["atr_key_sentence"])
        user = result["atr_key_sentence"] + (
            "\n\nKnown phrase meanings:\n- " + "\n- ".join(hints) if hints else ""
        )
        schema = {"type": "object", "properties": {"english": {"type": "string"}}, "required": ["english"]}
        translation, body = ollama_chat(TRANSLATE_PROMPT, user, schema, num_ctx=4096)
        result["atr_key_sentence_english"] = translation["english"]
        result["prompt_tokens"] += body.get("prompt_eval_count") or 0
        result["output_tokens"] += body.get("eval_count") or 0
    return result


def model_for(backend: str) -> str:
    if backend == "gemini":
        from app.ocr import GEMINI_MODEL
        return GEMINI_MODEL
    return OLLAMA_MODEL


def check_case(case_id: str, citizen_text: str, atr_text: str, backend: str = "gemini") -> dict:
    """Ask the model for a verdict on one grievance; returns every field, not just OUTPUT_FIELDS."""
    ask = ask_gemini if backend == "gemini" else ask_ollama
    result = ask(build_user_prompt(case_id, citizen_text, atr_text))
    meaning, is_resolved, formal_remark = VERDICT_INFO[result["verdict"]]
    return {
        "case_id": case_id,
        "verdict": result["verdict"],
        "verdict_meaning": meaning,
        "is_resolved": is_resolved,
        "final_action": result["final_action"],
        "complaint_summary": result["complaint_summary"],
        "atr_key_sentence": result["atr_key_sentence"],
        "atr_key_sentence_english": result["atr_key_sentence_english"],
        # A different grievance number in the ATR is certain; don't leave it to the model.
        "same_applicant": "NO" if other_grievance_numbers(case_id, atr_text) else result["same_applicant"],
        "reason": result["reason"],
        "formal_remark": formal_remark,
        "model": model_for(backend),
        "prompt_tokens": result["prompt_tokens"],
        "output_tokens": result["output_tokens"],
    }


def save_verdict(result: dict, verdict_dir: Path) -> tuple[dict, str]:
    """Write <id>Verdict.json and the workbook row; returns (saved fields, Excel note)."""
    saved = {field: result[field] for field in OUTPUT_FIELDS}
    verdict_dir.mkdir(parents=True, exist_ok=True)
    verdict_path = verdict_dir / f"{result['case_id']}Verdict.json"
    verdict_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        update_workbook([saved], verdict_dir / WORKBOOK_NAME)
        return saved, "added to Excel"
    except PermissionError as exc:
        return saved, f"Excel not updated yet: {exc}"


def main():
    parser = argparse.ArgumentParser(description="Compare Citizen grievances with ATRs using an LLM.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="PGRS_Output folder.")
    parser.add_argument("--verdict-dir", type=Path, default=DEFAULT_VERDICT_DIR, help="Folder for <id>Verdict.json files.")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Only check this grievance folder (repeatable), e.g. --case 379609",
    )
    parser.add_argument("--force", action="store_true", help="Re-check even if the verdict already exists.")
    parser.add_argument(
        "--backend",
        choices=("gemini", "ollama"),
        default="gemini",
        help="gemini (default, reads Kannada well) or ollama (local and free, but much less accurate).",
    )
    args = parser.parse_args()

    model_name = model_for(args.backend)

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
    print(f"Checking {len(case_dirs)} grievance(s) with {model_name}")

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

        citizen_text = citizen_path.read_text(encoding="utf-8")
        atr_text = atr_path.read_text(encoding="utf-8")
        try:
            result = check_case(case_dir.name, citizen_text, atr_text, args.backend)
        except urllib.error.URLError as exc:
            if args.backend != "ollama":
                raise
            raise SystemExit(f"Could not reach Ollama at {OLLAMA_URL}: {exc}. Is `ollama serve` running?")
        except Exception as exc:
            print(f"    FAILED: {exc}")
            continue

        _, excel_note = save_verdict(result, verdict_dir)

        print(f"    {result['verdict']} ({time.time() - started:.1f}s)")
        print(f"    Final action: {result['final_action']}")
        print(f"    {excel_note}")

    verdicts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(verdict_dir.glob("*Verdict.json"))
    ]

    summary_path = verdict_dir / "verdicts.json"
    summary_path.write_text(json.dumps(verdicts, ensure_ascii=False, indent=2), encoding="utf-8")

    resolved = sum(item["is_resolved"] for item in verdicts)
    print(f"\nResolved: {resolved}, not resolved: {len(verdicts) - resolved}")
    print(f"All verdicts written to {summary_path}")

    # Catch-up: also adds any verdicts that could not be written earlier
    # (e.g. because the workbook was open in Excel at the time).
    try:
        sync_verdict_dir(verdict_dir)
    except PermissionError as exc:
        print(f"WARNING: {exc}")


if __name__ == "__main__":
    main()
