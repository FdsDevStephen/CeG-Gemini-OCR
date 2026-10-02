"""Decide whether each ATR (Action Taken Report) resolves the citizen's grievance.

Reads the text written by app.ocr:
    PGRS_Output/<grievance_id>/Citizen.txt, ATR.txt

Asks one of two models, chosen with --model:
    gemini  Gemini 3.6 Flash (default): most accurate, paid API
    gemma   Gemma 4 E2B on local Ollama: free, ~30 s per case

and writes the observation to a folder per model, in both Observation/ and
Folder and Txt/Observation/:
    Gemini/<grievance_id>Observation.json and a row in Gemini/Observations_Gemini.xlsx
    Gemma/<grievance_id>Observation.json  and a row in Gemma/Observations_Gemma.xlsx
with a copy of each workbook directly in Observation/ (not in Folder and Txt/Observation/).

Usage:
    python -m app.observation
    python -m app.observation --model gemma
    python -m app.observation --case 379609 --force
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.excel import sync_observation_dir, update_workbook, workbook_path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = PROJECT_ROOT / "PGRS_Output"
OBSERVATION_ROOTS = (PROJECT_ROOT / "Observation", PROJECT_ROOT / "Folder and Txt" / "Observation")

MODELS = {"gemini": "Gemini 3.6 Flash (accurate, paid)", "gemma": "Gemma 4 E2B (local, free)"}
GEMMA_MODEL = "gemma4:e2b"
OLLAMA_URL = "http://localhost:11434"

# verdict -> (plain meaning, is_resolved). Kept in code so every verdict
# always maps to the same answer, whatever the model writes.
VERDICT_INFO = {
    "RESOLVED": (
        "The citizen's request was fulfilled as asked.",
        True,
    ),
    "PARTIALLY_RESOLVED": (
        "Only part of the request was fulfilled, or it was approved but the work is still pending.",
        False,
    ),
    "PENDING": (
        "Nothing has been sanctioned or done yet; the office says it will act later "
        "(e.g. once the government fixes a target or releases funds).",
        False,
    ),
    "REDIRECTED": (
        "The office did nothing itself; it told the citizen to apply to, or forwarded the request to, "
        "another office (e.g. Gram Panchayat).",
        False,
    ),
    "REJECTED": (
        "The office refused the request (e.g. citizen found ineligible).",
        False,
    ),
    "CLOSED_WITHOUT_ACTION": (
        "The office closed the request without doing anything and without directing the citizen elsewhere.",
        False,
    ),
    "WRONG_ACTION": (
        "The office did something different from what the citizen asked for "
        "(e.g. built a bridge instead of a road).",
        False,
    ),
    "UNCLEAR": (
        "The documents are too unclear or incomplete to judge.",
        False,
    ),
}
VERDICTS = list(VERDICT_INFO)

# Fields saved to <id>Observation.json and the workbook. The model still fills in
# VERDICT_SCHEMA in full; the extra fields only help it reason.
OUTPUT_FIELDS = (
    "case_id", "is_resolved", "case_summary", "final_action_taken",
    "atr_key_sentence", "atr_key_sentence_english",
)

# Field order matters: the model writes them in this order, so it quotes and
# translates the ATR's decisive sentence before it is allowed to judge.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "case_summary": {"type": "string"},
        "atr_key_sentence": {"type": "string"},
        "atr_key_sentence_english": {"type": "string"},
        "final_action_taken": {"type": "string"},
        "same_applicant": {"type": "string", "enum": ["YES", "NO", "UNCLEAR"]},
        "verdict": {"type": "string", "enum": VERDICTS},
        "reason": {"type": "string"},
    },
    "required": [
        "case_summary", "atr_key_sentence", "atr_key_sentence_english",
        "final_action_taken", "same_applicant", "verdict", "reason",
    ],
    "propertyOrdering": [
        "case_summary", "atr_key_sentence", "atr_key_sentence_english",
        "final_action_taken", "same_applicant", "verdict", "reason",
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
- case_summary: one or two sentences on what the citizen asked for.
- atr_key_sentence: copy, word for word in the original language, the ATR sentence that
  states the outcome.
- atr_key_sentence_english: a faithful English translation of that sentence.
- final_action_taken: one plain sentence on what the office FINALLY did, based on that sentence,
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
    from app import ocr  # needs GEMINI_API_KEY, so only imported for this model

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
    return json.loads(response.text)


def ollama_chat(system: str, user: str, schema: dict, num_ctx: int) -> dict:
    payload = {
        "model": GEMMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "think": True,  # reasoning first makes Gemma noticeably more accurate
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Ollama at {OLLAMA_URL} ({exc}). Is Ollama running?") from exc
    return json.loads(body["message"]["content"])


def ask_gemma(user_prompt: str) -> dict:
    schema = {k: v for k, v in VERDICT_SCHEMA.items() if k != "propertyOrdering"}
    result = ollama_chat(SYSTEM_PROMPT, user_prompt, schema, num_ctx=16384)

    # A small local model sometimes copies (or romanises) the Kannada instead of
    # translating it; then translate that one sentence on its own. Only as a
    # fallback: on sentences it already got right, this tends to invent details.
    english = result["atr_key_sentence_english"]
    if KANNADA_RE.search(english) or not ENGLISH_WORD_RE.search(english):
        hints = outcome_phrases(result["atr_key_sentence"])
        user = result["atr_key_sentence"] + (
            "\n\nKnown phrase meanings:\n- " + "\n- ".join(hints) if hints else ""
        )
        schema = {"type": "object", "properties": {"english": {"type": "string"}}, "required": ["english"]}
        result["atr_key_sentence_english"] = ollama_chat(TRANSLATE_PROMPT, user, schema, num_ctx=4096)["english"]
    return result


def check_case(case_id: str, citizen_text: str, atr_text: str, model: str = "gemini") -> dict:
    """Ask the model about one grievance; returns OUTPUT_FIELDS."""
    ask = ask_gemini if model == "gemini" else ask_gemma
    result = ask(build_user_prompt(case_id, citizen_text, atr_text))
    _, is_resolved = VERDICT_INFO[result["verdict"]]
    result.update(case_id=case_id, is_resolved=is_resolved)
    return {field: result[field] for field in OUTPUT_FIELDS}


def observation_dirs(model: str) -> tuple[Path, ...]:
    """Each model keeps its own JSON files and workbook, e.g. Observation/Gemma."""
    return tuple(root / model.capitalize() for root in OBSERVATION_ROOTS)


def workbook_paths(folder: Path) -> list[Path]:
    """The model folder's workbook; the main Observation/ also keeps a copy next to the model folders."""
    paths = [workbook_path(folder)]
    if folder.parent == OBSERVATION_ROOTS[0]:
        paths.append(folder.parent / paths[0].name)
    return paths


def save_observation(observation: dict, model: str) -> str:
    """Write <id>Observation.json and the Excel row in the model's observation folders; returns a status note."""
    notes = []
    for folder in observation_dirs(model):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{observation['case_id']}Observation.json"
        path.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
        for workbook in workbook_paths(folder):
            try:
                update_workbook([observation], workbook)
            except PermissionError as exc:
                notes.append(str(exc))
    return "; ".join(notes) or "added to Excel"


def main():
    parser = argparse.ArgumentParser(description="Check each ATR against the citizen's grievance.")
    parser.add_argument("--model", choices=MODELS, default="gemini", help="gemini (default) or gemma.")
    parser.add_argument("--case", action="append", dest="cases", help="Only this grievance (repeatable).")
    parser.add_argument("--force", action="store_true", help="Re-check grievances that already have one.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if not TEXT_DIR.is_dir():
        raise SystemExit(f"Text folder not found: {TEXT_DIR} (run python -m app.ocr first)")
    case_dirs = sorted(p for p in TEXT_DIR.iterdir() if p.is_dir() and (not args.cases or p.name in args.cases))
    folders = observation_dirs(args.model)
    print(f"Checking {len(case_dirs)} grievance(s) with {MODELS[args.model]}")

    for index, case_dir in enumerate(case_dirs, start=1):
        label = f"[{index}/{len(case_dirs)}] {case_dir.name}"
        citizen_path, atr_path = case_dir / "Citizen.txt", case_dir / "ATR.txt"

        missing = [p.name for p in (citizen_path, atr_path) if not p.exists()]
        if missing:
            print(f"{label} -> skipped (missing {', '.join(missing)})")
            continue
        if (folders[0] / f"{case_dir.name}Observation.json").exists() and not args.force:
            print(f"{label} -> skipped (already checked)")
            continue

        print(f"{label} -> checking...", flush=True)
        started = time.time()
        try:
            observation = check_case(
                case_dir.name,
                citizen_path.read_text(encoding="utf-8"),
                atr_path.read_text(encoding="utf-8"),
                args.model,
            )
        except Exception as exc:
            print(f"    FAILED: {exc}")
            continue

        note = save_observation(observation, args.model)
        status = "Resolved" if observation["is_resolved"] else "Not resolved"
        print(f"    {status} ({time.time() - started:.0f}s), {note}")

    observation_dir = folders[0]
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(observation_dir.glob("*Observation.json"))
    ]
    (observation_dir / "observations.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resolved = sum(item["is_resolved"] for item in observations)
    print(f"\nResolved: {resolved}, not resolved: {len(observations) - resolved}")

    # Catch-up: adds rows that could not be written earlier (e.g. the workbook was open in Excel).
    for folder in folders:
        try:
            sync_observation_dir(folder, workbook_paths(folder))
        except PermissionError as exc:
            print(f"WARNING: {exc}")


if __name__ == "__main__":
    main()
