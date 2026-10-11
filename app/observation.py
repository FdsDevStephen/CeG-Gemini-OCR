"""Decide whether each ATR (Action Taken Report) resolves the citizen's grievance.

Reads the text written by app.ocr:
    PGRS_Output/<grievance_id>/Citizen.txt, ATR.txt

Asks one of two models, chosen with --model:
    gemma   Gemma 4 E2B on local Ollama (default): free, ~30-75 s per case
    gemini  Gemini 3.6 Flash: most accurate, paid API
    qwen3   Qwen3 14B on Ollama (local unless QWEN3_OLLAMA_URL is set in .env)

and writes the observation to a folder per model, in both Observation/ and
Folder and Txt/Observation/:
    Gemini/<grievance_id>Observation.json and a row in Gemini/Observations_Gemini.xlsx
    Gemma/<grievance_id>Observation.json  and a row in Gemma/Observations_Gemma.xlsx
with a copy of each workbook directly in Observation/ (not in Folder and Txt/Observation/).

Usage:
    python -m app.observation
    python -m app.observation --model gemini
    python -m app.observation --case 379609 --force
"""

import argparse
import os
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from app.excel import sync_observation_dir, update_workbook, workbook_path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = PROJECT_ROOT / "PGRS_Output"
OBSERVATION_ROOTS = (PROJECT_ROOT / "Observation", PROJECT_ROOT / "Folder and Txt" / "Observation")

# The first model is the default, here and in the Streamlit app.
MODELS = {
    "gemma": "Gemma 4 E2B (local, free)",
    "gemini": "Gemini 3.6 Flash (accurate, paid)",
    "gemma-e4b": "Gemma 4 E4B-it (remote server)",
    "qwen14b": "Qwen3 14B (remote server)",
    "qwen3": "Qwen3 14B (Ollama)",
}
GEMMA_MODEL = "gemma4:e2b"
OLLAMA_URL = "http://localhost:11434"
load_dotenv()
QWEN3_MODEL = os.getenv("QWEN3_MODEL", "qwen3:14b")
QWEN3_OLLAMA_URL = os.getenv("QWEN3_OLLAMA_URL", OLLAMA_URL).rstrip("/")  # or another machine's
GEMMA_E4B_URL = "http://101.53.137.13:8004/gemma/generate"
# generate_greedy: temperature 0 and no thinking, so the same text always gets the same
# verdict, in ~10 s instead of minutes.
QWEN14B_URL = "http://101.53.137.13:8004/qwen13/generate_greedy"
QWEN_ATTEMPTS = 3

# verdict -> (plain meaning, is_resolved, star rating of the service). Kept in code
# so every verdict always maps to the same answers, whatever the model writes.
# Rating: 3 = done; 2 = partly done, registered and being processed, sent to another
# office, or more documents asked for; 1 = no action (pending, refused, closed, wrong
# action or unclear).
VERDICT_INFO = {
    "RESOLVED": (
        "The citizen's request was fulfilled as asked.",
        True,
        3,
    ),
    "PARTIALLY_RESOLVED": (
        "Only part of the request was fulfilled, or it was approved but the work is still pending.",
        False,
        2,
    ),
    "IN_PROCESS": (
        "The office has taken up the request itself (e.g. registered it with a request number "
        "in its software) and is processing it, but it is not fulfilled yet.",
        False,
        2,
    ),
    "PENDING": (
        "Nothing has been sanctioned or done yet; the office says it will act later "
        "(e.g. once the government fixes a target or releases funds).",
        False,
        1,
    ),
    "REDIRECTED": (
        "The office did nothing itself; it told the citizen to apply to, or forwarded the request to, "
        "another office (e.g. Gram Panchayat).",
        False,
        2,
    ),
    "DOCUMENTS_REQUESTED": (
        "The office asked the citizen to submit more or missing documents before it can act.",
        False,
        2,
    ),
    "REJECTED": (
        "The office refused the request (e.g. citizen found ineligible).",
        False,
        1,
    ),
    "CLOSED_WITHOUT_ACTION": (
        "The office closed the request without doing anything and without directing the citizen elsewhere.",
        False,
        1,
    ),
    "WRONG_ACTION": (
        "The office did something different from what the citizen asked for "
        "(e.g. built a bridge instead of a road).",
        False,
        1,
    ),
    "UNCLEAR": (
        "The documents are too unclear or incomplete to judge.",
        False,
        1,
    ),
}
VERDICTS = list(VERDICT_INFO)

# Fields saved to <id>Observation.json and the workbook. The model still fills in
# VERDICT_SCHEMA in full; the extra fields only help it reason.
OUTPUT_FIELDS = (
    "case_id", "is_resolved", "rating", "scheme", "scheme_english", "case_summary", "final_action_taken",
    "atr_key_sentence", "atr_key_sentence_english",
)

# Field order matters: the model writes them in this order, so it names the
# scheme first, and quotes and translates the ATR's decisive sentence before
# it is allowed to judge.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "scheme": {"type": "string"},
        "scheme_english": {"type": "string"},
        "case_summary": {"type": "string"},
        "atr_key_sentence": {"type": "string"},
        "atr_key_sentence_english": {"type": "string"},
        "final_action_taken": {"type": "string"},
        "same_applicant": {"type": "string", "enum": ["YES", "NO", "UNCLEAR"]},
        "verdict": {"type": "string", "enum": VERDICTS},
        "reason": {"type": "string"},
    },
    "required": [
        "scheme", "scheme_english", "case_summary", "atr_key_sentence", "atr_key_sentence_english",
        "final_action_taken", "same_applicant", "verdict", "reason",
    ],
    "propertyOrdering": [
        "scheme", "scheme_english", "case_summary", "atr_key_sentence", "atr_key_sentence_english",
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
    (r"ಸಲ್ಲಿಸಿದಲ್ಲಿ", "\"if you submit (the documents)\": the office ASKED for documents first (DOCUMENTS_REQUESTED)"),
    (r"ಖಾತೆಗೆ\s*(?:ಪಾವತಿ|ಜಮೆ)", "the money HAS BEEN PAID / credited to the citizen's account (done)"),
    (r"ಈಗಾಗಲೇ", "ALREADY: check what was already done - work done for the citizen, or only a notice "
                "telling the citizen to act (that is not done)"),
    (r"ಕರೆಯನ್ನು\s*ಸ್ವೀಕರಿಸಿರುವುದಿಲ್ಲ", "the citizen did NOT answer the office's phone call"),
    (r"ಸಂಬಂಧವಿರುವುದಿಲ್ಲ", "the applicant says they have NO connection to the application"),
    (r"ಅರ್ಹರಿರುವುದಿಲ್ಲ|ಅನರ್ಹ", "the citizen is NOT eligible"),
    # \S, not \w: Python's \w does not match Kannada vowel signs.
    (r"(?:ಪಂಚಾಯಿತಿ|ಕಛೇರಿ|ಇಲಾಖೆ)\S*\s*(?:ಕೋರಿಕೆ|ಅರ್ಜಿ|ಮನವಿ)\S*\s*ಸಲ್ಲಿಸ|ಸಲ್ಲಿಸುವಂತೆ\s*ಸೂಚಿಸ",
     "the citizen was TOLD TO APPLY to another office (e.g. the Gram Panchayat)"),
    (r"ಕಳುಹಿಸಲಾಗಿದೆ|ವರ್ಗಾಯಿಸಲಾಗಿದೆ|ರವಾನಿಸಲಾಗಿದೆ", "the request was FORWARDED to another office"),
    (r"ಪ್ರಗತಿಯಲ್ಲಿದೆ", "the work is IN PROGRESS"),
    (r"ತಂತ್ರಾಂಶದಲ್ಲಿ\s*(?:\S+\s*){0,3}?(?:ಅಳವಡಿಸಲಾಗಿ|ನೋಂದಾಯಿಸಲಾಗಿ|ನೋಂದಣಿ)",
     "the request was ENTERED / REGISTERED in the software: taken up, NOT fulfilled yet (IN_PROCESS)"),
    (r"ಕ್ರಮ\s*(?:ವಹಿಸಲಾಗುತ್ತದೆ|ಕೈಗೊಳ್ಳಲಾಗುತ್ತದೆ)", "action WILL be taken (future; nothing done yet)"),
    (r"ತಡೆ\s*ಹಿಡಿಯಲಾಗಿ", "the pension/benefit is WITHHELD / on hold (NOT restarted)"),
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

# "A, B", "A / B", "A ಮತ್ತು B" (and): several schemes instead of one.
SCHEME_LIST_RE = re.compile(r"[,/;]| ಮತ್ತು ")

SCHEME_PROMPT = """
You name the one Karnataka government scheme or service a citizen's grievance is about.
Answer with exactly ONE scheme, never a list.
scheme: its name in Kannada script. scheme_english: the same name in English.
If the citizen ticked a scheme on the form, that is the scheme. If nothing was ticked, the
form's printed options are NOT the request: base it on the Request and Outcome lines.
If the request is not a scheme (e.g. a road, drainage, a loan, a quarry lease), name that
service in Kannada.
""".strip()

TRANSLITERATE_PROMPT = """
Write the given Karnataka government scheme or service name in Kannada script, as it is
officially written in Kannada. Keep the same scheme; do not replace it with another one.
""".strip()

SYSTEM_PROMPT = """
You are a grievance redressal auditor for a government Public Grievance Redressal System (PGRS).

You receive two OCR'd documents, usually in Kannada:
1. CITIZEN: the grievance or request submitted by the citizen. It is often a printed
   application form listing many schemes; ONLY the ticked (✓) item(s) and any attached
   letter are what the citizen actually asked for. Ignore the options that are not ticked.
   OCR often loses the tick marks and the handwriting. If no tick is visible, do NOT assume
   the first printed options (e.g. the pension schemes) were chosen: take the request from
   the handwritten "ಇತರೆ" (other) line, the citizen's letter, or the ATR's own restatement of
   what the citizen asked for (e.g. "ತಾವು ... ಎಂದು ಅರ್ಜಿ ಸಲ್ಲಿಸಿದ್ದು" = you applied saying ...).
2. ATR: the Action Taken Report written by the government office in response.

Read the ATR carefully. Its first sentences usually restate the request or the
background problem; the outcome is the sentence that says what WAS done, was NOT
done, or WILL be done. Watch the Kannada verb endings:
- ...ಲಾಗಿದೆ / ...ಲಾಗಿರುತ್ತದೆ / ...ಮಾಡಿದೆ = has been done (completed)
- ...ಲಾಗುವುದು / ...ಲಾಗುತ್ತದೆ = will be done (future; nothing done yet)
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
- ತಂತ್ರಾಂಶದಲ್ಲಿ ಅಳವಡಿಸಲಾಗಿರುತ್ತದೆ / ನೋಂದಾಯಿಸಲಾಗಿದೆ = entered / registered in the software:
  the office has taken the request up but not fulfilled it yet (IN_PROCESS)
- ತಡೆ ಹಿಡಿಯಲಾಗಿದೆ = withheld / put on hold (NOT restarted)
- ಹಿಂಬರಹ = endorsement / reply letter; ವಿಲೇವಾರಿ = disposed
- ಮನೆ = house; ನಿವೇಶನ / ಜಾಗ = site / plot; ವಸತಿ ಯೋಜನೆ = housing scheme

Fill in, in English unless a field says otherwise:
- scheme: the exact government scheme or service the request is about, in Kannada, e.g.
  ಸಂಧ್ಯಾ ಸುರಕ್ಷಾ ಪಿಂಚಣಿ ಯೋಜನೆ, ವಿಧವಾ ಪಿಂಚಣಿ ಯೋಜನೆ, ಗೃಹ ಲಕ್ಷ್ಮಿ ಯೋಜನೆ, ವಸತಿ ಯೋಜನೆ.
  If either document names it, copy that name as written. If neither does, infer the most
  specific scheme yourself from the request, the ATR and the applicant's details: e.g. a
  stopped pension of an elderly person -> ಸಂಧ್ಯಾ ಸುರಕ್ಷಾ ಪಿಂಚಣಿ ಯೋಜನೆ; a widow's pension ->
  ವಿಧವಾ ಪಿಂಚಣಿ ಯೋಜನೆ; a house or site -> the housing scheme named, else ವಸತಿ ಯೋಜನೆ.
  Prefer a named scheme over a generic word like ಪಿಂಚಣಿ alone. Name exactly ONE scheme:
  never list several, and never copy the form's list of options. If a ticked form item is
  given under CHECKS DONE IN CODE, that is the scheme. If the request is not a scheme
  (e.g. a road, drainage, a quarry lease), name the service in Kannada instead.
  Always write this field in Kannada script.
- scheme_english: the English name of that scheme, e.g. "Sandhya Suraksha Pension Scheme
  (old-age pension)".
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
  (citizen asked for a road and the road was built; a stopped pension was restarted;
  the money asked for has already been paid to the citizen's account).
  Only registering the request, entering it in a software or forwarding it for processing
  is NOT RESOLVED: something must actually have been done or given to the citizen.
  When the ATR answers many applicants with different outcomes, only this citizen's own
  entry counts; if CHECKS DONE IN CODE gives that entry, follow it.
  Only telling the citizen to do something themselves (pay a fee, do the work at their own
  cost, apply online or elsewhere) is NOT RESOLVED, even when that notice has "already" been
  issued (ಈಗಾಗಲೇ ಸೂಚನಾ ಪತ್ರ ನೀಡಲಾಗಿರುತ್ತದೆ).
- PARTIALLY_RESOLVED: only part was fulfilled, or it was sanctioned but the work is pending.
- IN_PROCESS: the office has taken up the request itself and started processing it, but it
  is not fulfilled yet, e.g. it was entered / registered in the office's software with a
  request number, or a survey or inspection for it has been ordered. Choose this, not
  PENDING, when such a concrete step for THIS request is reported. Registering the
  complaint in the grievance portal (IPGRS / PGRS / ಕುಂದುಕೊರತೆ ತಂತ್ರಾಂಶ) with an order to
  dispose of it by a date also counts as IN_PROCESS: nothing is done yet, but it is taken up.
- PENDING: nothing has been sanctioned or done yet; the office says it will act later
  (e.g. after a target is fixed or funds are released). This is NOT partial resolution.
- REDIRECTED: the office did not act itself; it told the citizen to apply elsewhere or
  forwarded the request to another office or Gram Panchayat. This is NOT partial resolution.
- DOCUMENTS_REQUESTED: the office asked the citizen to submit more or missing documents
  (e.g. ದಾಖಲೆಗಳು ಲಗತ್ತಿಸಿರುವುದಿಲ್ಲ ... ಸಲ್ಲಿಸಿದಲ್ಲಿ ... ಕ್ರಮ ಕೈಗೊಳ್ಳಲಾಗುವುದು = documents not
  attached; if you submit them, action will be taken). Choose this, not PENDING or
  CLOSED_WITHOUT_ACTION, even if the request was closed meanwhile.
- REJECTED: the office refused the request, e.g. the citizen is not eligible.
- CLOSED_WITHOUT_ACTION: the request was closed/disposed (ಹಿಂಬರಹ / ವಿಲೇವಾರಿ) with no action
  and without telling the citizen where else to go.
- WRONG_ACTION: the office did something different from what was asked
  (citizen asked for a road, but a bridge was built). Do not choose this just because the
  ATR's request differs from a printed form option with no visible tick.
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


def ocr_tables(text: str) -> list[list[list[str]]]:
    """Every markdown table in the OCR text, as rows of cells (the --- separator row dropped)."""
    tables, rows = [], []
    for line in text.splitlines() + [""]:
        # Gemini writes "| a | b |"; Indic-OCR writes "a | b | c".
        if line.strip().startswith("|") or line.count(" | ") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-*:?", c) for c in cells):
                rows.append(cells)
        elif rows:
            tables.append(rows)
            rows = []
    return tables


def one_digit_apart(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def applicant_entry(case_id: str, citizen_text: str, atr_text: str) -> str | None:
    """This citizen's own entry in an ATR that answers many applicants in one list.

    The list is found by grievance number; OCR often gets one digit wrong, so a number one
    digit off also counts when the entry's name appears in the citizen's document. Each
    applicant can be a row, or (as OCR often lays it out) a column.
    """
    citizen_words = set(re.findall(r"\S{3,}", citizen_text))
    for table in ocr_tables(atr_text):
        width = max(len(row) for row in table)
        rows = [row + [""] * (width - len(row)) for row in table]
        for r, row in enumerate(rows):
            numbers = [c for c in row if re.fullmatch(r"\d{6}", c)]
            if len(numbers) < 2:
                continue
            columns_are_applicants = True
            break
        else:
            columns_are_applicants = False
        entries = list(zip(*rows)) if columns_are_applicants else rows
        if len(entries) < 3:
            continue
        exact = [e for e in entries if case_id in e]
        near = [
            e for e in entries
            if any(one_digit_apart(case_id, c) for c in e)
            and any(w in citizen_words for c in e if not c.isdigit() for w in c.split() if len(w) >= 3)
        ]
        found = exact or near
        if len(found) == 1:
            return " | ".join(c for c in found[0] if c)
    return None


def build_user_prompt(case_id: str, citizen_text: str, atr_text: str) -> str:
    ticked = ticked_items(citizen_text)
    notes = [
        "Ticked on the citizen's form: " + "; ".join(ticked)
        if ticked else "No ticked form option was detected; rely on the citizen's letter."
    ]
    phrases = outcome_phrases(atr_text)
    if phrases:
        notes.append("Outcome phrases found in the ATR (reliable translations):\n- " + "\n- ".join(phrases))
    entry = applicant_entry(case_id, citizen_text, atr_text)
    if entry:
        notes.append(
            "The ATR answers many applicants in one list. This citizen's own entry in that list "
            f"(serial number, grievance number, name, ..., the office's remark):\n{entry}\n"
            "Decide the verdict from THIS entry's serial number and remark, using what the ATR "
            "says for that range of serial numbers - not from what was done for other applicants."
        )
    others = other_grievance_numbers(case_id, atr_text) if not entry else []
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

    response = ocr.gemini_client().models.generate_content(
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


def ollama_chat(system: str, user: str, schema: dict, num_ctx: int,
                model: str = GEMMA_MODEL, url: str = OLLAMA_URL) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "think": True,  # reasoning first makes Gemma (and Qwen3) noticeably more accurate
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    request = urllib.request.Request(
        f"{url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Ollama at {url} ({exc}). Is Ollama running?") from exc
    return json.loads(body["message"]["content"])


def qwen3_chat(system: str, user: str, schema: dict, num_ctx: int) -> dict:
    """Same as ollama_chat, with Qwen3 14B (pull it first: ollama pull qwen3:14b)."""
    return ollama_chat(system, user, schema, num_ctx, QWEN3_MODEL, QWEN3_OLLAMA_URL)


def qwen_chat(system: str, user: str, schema: dict, num_ctx: int) -> dict:
    """Same as ollama_chat, on the remote Qwen3 14B server. One plain prompt and no JSON
    mode, but unlike the E4B server its JSON and Kannada come back clean."""
    keys = ", ".join(
        f'"{k}"' + (f" (one of: {', '.join(v['enum'])})" if "enum" in v else "")
        for k, v in schema["properties"].items()
    )
    prompt = (
        f"{system}\n\n{user}\n\n"
        f"Reply with ONLY one JSON object, with exactly these string keys in this order: {keys}."
    )
    request = urllib.request.Request(
        QWEN14B_URL,
        data=json.dumps({"prompt": prompt, "max_tokens": 4096}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # The test server drops connections and returns 500/504 now and then, and a sampled
    # reply can come back without its JSON; a retry after a short wait usually works.
    for attempt in range(1, QWEN_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                text = json.loads(response.read().decode("utf-8"))["text"]
            return parse_qwen_reply(text, schema)
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError, RuntimeError) as exc:
            if attempt == QWEN_ATTEMPTS:
                raise RuntimeError(f"Qwen server at {QWEN14B_URL} failed {attempt} times ({exc})") from exc
            time.sleep(15 * attempt)


def parse_qwen_reply(text: str, schema: dict) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"No JSON in the model's reply (thinking may have used up max_tokens): {text[-200:]!r}")
    result = json.loads(match.group(), strict=False)
    missing = [k for k in schema["required"] if k not in result]
    if missing:
        raise RuntimeError(f"Reply is missing {', '.join(missing)}")
    for key, spec in schema["properties"].items():
        if "enum" in spec and result[key] not in spec["enum"]:
            raise RuntimeError(f"{key} is not one of the labels: {result[key]!r}")
    return result


def after_label(line: str, key: str) -> str | None:
    """The rest of the line if it starts with the key's letters, whatever is between them
    ("VERDICT_RESOLVED", "REASON_The ..."), or with a misspelt label and a colon
    ("ATR_KEY_SENTENCE_ENGHISH: ..."); else None."""
    letters, i = key.replace("_", "").upper(), 0
    label, colon, value = line.partition(":")
    if colon and difflib.SequenceMatcher(None, re.sub(r"[^A-Z]", "", label.upper()), letters).ratio() >= 0.85:
        return value.strip() or None
    for position, char in enumerate(line):
        if i == len(letters):
            return line[position:].lstrip(" _\\-:*").strip() or None
        if char.upper() == letters[i]:
            i += 1
        elif char.isalpha():
            return None
    return None


def remote_gemma_chat(system: str, user: str, schema: dict, num_ctx: int) -> dict:
    """Same as ollama_chat, on the remote Gemma 4 E4B server. It takes one plain prompt
    and has no JSON mode, and its JSON comes back broken (misspelt keys, missing
    quotes), so it answers in labelled lines instead: "VERDICT: RESOLVED"."""
    fields = list(schema["properties"])
    lines = "\n".join(
        f"{k.upper()}: <{' or '.join(v['enum'])}>" if "enum" in v
        else f"{k.upper()}: <number>" if k.endswith("_number") else f"{k.upper()}: <text>"
        for k, v in schema["properties"].items()
    )
    prompt = (
        f"{system}\n\n{user}\n\n"
        "Answer in exactly these lines, in this order, each on ONE line starting with its label. "
        f"No JSON, no markdown, nothing else:\n{lines}"
    )
    payload = {"prompt": prompt, "max_tokens": 4096, "temperature": 0}
    request = urllib.request.Request(
        GEMMA_E4B_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            text = json.loads(response.read().decode("utf-8"))["text"]
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Gemma server at {GEMMA_E4B_URL} ({exc})") from exc

    # Labels longest first, so SCHEME_ENGLISH is not read as SCHEME; "_" may come back as " ", "-", "\_" or "_ ".
    label_re = re.compile(
        r"^[\s*#>-]*(" + "|".join(k.replace("_", r"[\\ _-]+") for k in sorted(fields, key=len, reverse=True))
        + r")[^:\n]{0,40}?:\s*(.*)$",  # it sometimes adds to a label: "SAME_APPLICANT_YES:"
        re.IGNORECASE | re.MULTILINE,
    )
    result = {}
    for label, value in label_re.findall(text):
        key = re.sub(r"[\\ _-]+", "_", label.lower())
        result.setdefault(key, value.strip().strip("*\"'“”` ").strip())
    # Near the end of a reply the labels can lose their colon too ("VERDICT_RESOLVED").
    for key in fields:
        if key not in result:
            value = next((v for v in map(lambda line: after_label(line, key), text.splitlines()) if v), None)
            if value:
                result[key] = value.strip("*\"'“”` ")
    for key, spec in schema["properties"].items():
        if "enum" in spec and key in result:
            word = re.sub(r"[^A-Z_]", "", result[key].upper().replace(" ", "_"))
            result[key] = next((e for e in spec["enum"] if e == word), None) or next(
                (e for e in sorted(spec["enum"], key=len, reverse=True) if e in word), None
            ) or next(iter(difflib.get_close_matches(word, spec["enum"], n=1, cutoff=0.7)), result[key])
            if result[key] not in spec["enum"]:
                raise RuntimeError(f"{key} is not one of the labels: {result[key]!r}")
    # A skipped text line is left empty (ask_gemma re-asks for an empty translation or
    # scheme); a missing label (verdict, same_applicant) fails the case.
    missing = [k for k in schema["required"] if k not in result and "enum" in schema["properties"][k]]
    if missing:
        raise RuntimeError(f"Reply is missing {', '.join(missing)}: {text[:300]!r}")
    return {k: result.get(k, "") for k in fields}


def ask_gemma_scheme(result: dict, ticked: list[str], chat=ollama_chat) -> dict:
    """Fix Gemma's scheme: write a single English name in Kannada, or pick one from a list."""
    if not SCHEME_LIST_RE.search(result["scheme"]):
        schema = {"type": "object", "properties": {"scheme": {"type": "string"}}, "required": ["scheme"]}
        kannada = chat(TRANSLITERATE_PROMPT, result["scheme"], schema, num_ctx=2048)["scheme"]
        return {"scheme": kannada, "scheme_english": result["scheme_english"]}

    user = (
        f"Request: {result['case_summary']}\n"
        f"Outcome: {result['final_action_taken']}\n"
        f"Ticked on the citizen's form: {'; '.join(ticked) or 'nothing detected'}\n"
        f"Scheme names seen in the documents (may be the form's whole list of options): {result['scheme']}"
    )
    schema = {
        "type": "object",
        "properties": {"scheme": {"type": "string"}, "scheme_english": {"type": "string"}},
        "required": ["scheme", "scheme_english"],
    }
    return chat(SCHEME_PROMPT, user, schema, num_ctx=4096)


def ask_gemma(user_prompt: str, ticked: list[str], chat=ollama_chat) -> dict:
    schema = {k: v for k, v in VERDICT_SCHEMA.items() if k != "propertyOrdering"}
    result = chat(SYSTEM_PROMPT, user_prompt, schema, num_ctx=16384)

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
        result["atr_key_sentence_english"] = chat(TRANSLATE_PROMPT, user, schema, num_ctx=4096)["english"]

    # It also tends to list every scheme on the form, or write the scheme in English;
    # then ask for the one scheme on its own.
    if not KANNADA_RE.search(result["scheme"]) or SCHEME_LIST_RE.search(result["scheme"]):
        result.update(ask_gemma_scheme(result, ticked, chat))
    return result


# The remote E4B server garbles any Kannada it writes (letters of Telugu, Hindi or
# Japanese mixed in), though it reads Kannada well. So for that model code copies the
# Kannada fields: it picks the ATR sentence and the scheme by number.
SCHEMES = [
    ("ಸಂಧ್ಯಾ ಸುರಕ್ಷಾ ಯೋಜನೆ", "Sandhya Suraksha Scheme (old-age pension)"),
    ("ವಿಧವಾ ವೇತನ ಯೋಜನೆ", "Widow Pension Scheme"),
    ("ಅಂಗವಿಕಲರ ಮಾಸಾಶನ ಯೋಜನೆ", "Disability Pension Scheme"),
    ("ಇಂದಿರಾ ಗಾಂಧಿ ರಾಷ್ಟ್ರೀಯ ವೃದ್ಧಾಪ್ಯ ವೇತನ", "Indira Gandhi National Old Age Pension"),
    ("ಮನಸ್ವಿನಿ ಯೋಜನೆ", "Manaswini Scheme (pension for unmarried or divorced women)"),
    ("ಮೈತ್ರಿ ಯೋಜನೆ", "Mythri Scheme (pension for transgender persons)"),
    ("ಇತರೆ ಪಿಂಚಣಿ ಯೋಜನೆ", "Other Pension Scheme"),
    ("ವಸತಿ ಯೋಜನೆ", "Housing Scheme (site / house allotment)"),
    ("ಪಹಣಿ ತಿದ್ದುಪಡಿ", "Pahani (RTC land record) correction"),
    ("ಪೋಡಿ", "Podi (land survey / subdivision)"),
    ("ಭೂ ದಾಖಲೆಗಳ ದುರಸ್ತಿ", "Land record correction (durasthi)"),
    ("ಖಾತೆ ಬದಲಾವಣೆ", "Khata transfer"),
    ("ಹೊಸ ಪಡಿತರ ಚೀಟಿ", "New ration card"),
    ("ರಸ್ತೆ ಅಭಿವೃದ್ಧಿ", "Road development"),
    ("ಸೇತುವೆ ನಿರ್ಮಾಣ", "Bridge construction"),
    ("ಕುಡಿಯುವ ನೀರು", "Drinking water"),
    ("ಚರಂಡಿ ಮತ್ತು ನೈರ್ಮಲ್ಯ", "Drainage and sanitation"),
    ("ವಿದ್ಯುತ್ ಸಂಪರ್ಕ", "Electricity connection / power line"),
    ("ಸ್ಮಶಾನ ಅಭಿವೃದ್ಧಿ", "Cemetery development"),
    ("ದೇವಸ್ಥಾನ ಅಭಿವೃದ್ಧಿ", "Temple development / renovation"),
    ("ಸರ್ಕಾರಿ ಶಾಲೆ / ಅಂಗನವಾಡಿ ಅಭಿವೃದ್ಧಿ", "Government school / anganwadi development"),
    ("ಮಹಾತ್ಮ ಗಾಂಧಿ ರಾಷ್ಟ್ರೀಯ ಗ್ರಾಮೀಣ ಉದ್ಯೋಗ ಖಾತ್ರಿ ಯೋಜನೆ", "MGNREGA (job scheme)"),
    ("ಕೆರೆ ತುಂಬಿಸುವ ಯೋಜನೆ", "Lake filling / irrigation"),
    ("ಗೃಹ ಲಕ್ಷ್ಮಿ ಯೋಜನೆ", "Gruha Lakshmi Scheme"),
    ("ಗೃಹ ಜ್ಯೋತಿ ಯೋಜನೆ", "Gruha Jyothi Scheme (free electricity)"),
    ("ಶಕ್ತಿ ಯೋಜನೆ", "Shakti Scheme (free bus travel)"),
    ("ಅನ್ನ ಭಾಗ್ಯ ಯೋಜನೆ", "Anna Bhagya Scheme (ration rice)"),
    ("ಯುವ ನಿಧಿ ಯೋಜನೆ", "Yuva Nidhi Scheme"),
    ("ಸಾರಿಗೆ ಸೇವೆ / ಬಸ್ ಪಾಸ್", "Transport (KSRTC bus service / bus pass)"),
    ("ಪೊಲೀಸ್ ಇಲಾಖೆ ಸೇವೆ", "Police department service"),
    ("ಕಲ್ಲುಗಣಿ ಗುತ್ತಿಗೆ", "Quarry / mining lease"),
    ("ಪ್ರಮಾಣ ಪತ್ರ", "Certificate (caste, income, project-displaced, etc.)"),
]

E4B_NOTE = """
For this reply two fields change, so that you never write Kannada:
- Leave out scheme; give only SCHEME_ENGLISH.
- ATR_KEY_SENTENCE_NUMBER instead of atr_key_sentence: the [number] of the ATR sentence that
  states the outcome. Do not copy the sentence.
- Write every other field in English.
""".strip()

VERDICT_GUIDE = (
    "You judge whether a government office resolved a citizen's grievance, from the facts below.\n\n"
    + SYSTEM_PROMPT[SYSTEM_PROMPT.index("Verdict labels:"):]
)

TABLE_OR_BLANK_RE = re.compile(r"\n\s*\n|\n(?=\s*\|)|(?<=\|)\s*\n")
SENTENCE_END_RE = re.compile(r"(?<=[.।!?])\s+")


def atr_sentences(atr_text: str) -> list[str]:
    """The ATR split into sentences, with OCR line breaks inside a sentence joined up."""
    sentences = []
    for block in TABLE_OR_BLANK_RE.split(atr_text):
        sentences += [" ".join(s.split()) for s in SENTENCE_END_RE.split(" ".join(block.split()))]
    return [s for s in sentences if s]


def picked_number(value: str, count: int) -> int:
    """The number the model picked, or 0 if it gave none in 1..count."""
    match = re.search(r"\d+", value)
    number = int(match.group()) if match else 0
    return number if 1 <= number <= count else 0


def is_placeholder(text: str) -> bool:
    """A degraded line can come back as the template itself ("[Insert a plain sentence ...]")."""
    return text.lstrip().startswith(("[", "<")) or "insert" in text.lower()[:20]


def known_scheme(name: str) -> tuple[str, str] | None:
    """(Kannada, English) of the listed scheme the English name means, if any."""
    def clean(text):
        text = re.sub(r"\byoj\w*", "scheme", text.lower().split("(")[0])  # Yojana / Yojane
        return re.sub(r"[^a-z ]", " ", text).split()

    words = clean(name)
    best, best_ratio = None, 0.0
    for kn, en in SCHEMES:
        ratio = difflib.SequenceMatcher(None, " ".join(words), " ".join(clean(en))).ratio()
        if ratio > best_ratio:
            best, best_ratio = (kn, en), ratio
    return best if best_ratio >= 0.75 else None


def ask_gemma_e4b(case_id: str, citizen_text: str, atr_text: str) -> dict:
    sentences = atr_sentences(atr_text)
    numbered_atr = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences, start=1))
    user_prompt = build_user_prompt(case_id, citizen_text, numbered_atr).replace("\n\nReturn the JSON verdict.", "")
    fields = {
        "scheme_english": {"type": "string"},
        "case_summary": {"type": "string"},
        "atr_key_sentence_number": {"type": "string"},
        **{k: v for k, v in VERDICT_SCHEMA["properties"].items()
           if k not in ("scheme", "scheme_english", "case_summary", "atr_key_sentence")},
    }
    schema = {"type": "object", "properties": fields, "required": list(fields)}
    result = remote_gemma_chat(f"{SYSTEM_PROMPT}\n\n{E4B_NOTE}", user_prompt, schema, num_ctx=16384)

    sentence = picked_number(result["atr_key_sentence_number"], len(sentences))
    if not sentence and sentences:  # no usable number: ask for it on its own, in a short prompt
        user = (
            f"ATR sentences:\n{numbered_atr}\n\nOutcome: {result['final_action_taken']}\n\n"
            "Which numbered sentence states this outcome?"
        )
        schema = {"type": "object", "properties": {"atr_key_sentence_number": {"type": "string"}},
                  "required": ["atr_key_sentence_number"]}
        sentence = picked_number(
            remote_gemma_chat("", user, schema, 4096)["atr_key_sentence_number"], len(sentences)
        )
    result["atr_key_sentence"] = sentences[sentence - 1] if sentence else ""

    english = result["atr_key_sentence_english"]
    if result["atr_key_sentence"] and (
        KANNADA_RE.search(english) or not ENGLISH_WORD_RE.search(english) or is_placeholder(english)
    ):
        hints = outcome_phrases(result["atr_key_sentence"])
        user = result["atr_key_sentence"] + (
            "\n\nKnown phrase meanings:\n- " + "\n- ".join(hints) if hints else ""
        )
        schema = {"type": "object", "properties": {"english": {"type": "string"}}, "required": ["english"]}
        result["atr_key_sentence_english"] = remote_gemma_chat(TRANSLATE_PROMPT, user, schema, 4096)["english"]
    if is_placeholder(result["final_action_taken"]):
        result["final_action_taken"] = result["atr_key_sentence_english"]

    # In the long reply it mostly answers "Other Pension Scheme", and its numbers are not
    # reliable; asked on its own for the English name, it does well. Code matches the list.
    ticked = ticked_items(citizen_text)
    user = (
        f"Request: {result['case_summary']}\n"
        f"Outcome: {result['final_action_taken']}\n"
        f"Ticked on the citizen's form: {'; '.join(ticked) or 'nothing detected'}\n\n"
        "Known schemes and services:\n- " + "\n- ".join(en for _, en in SCHEMES) + "\n\n"
        "Name the one scheme or service the request is about, in English. Use the name from the "
        "list if one fits; otherwise name the service (e.g. barrage gate repair)."
    )
    schema = {"type": "object", "properties": {"scheme_english": {"type": "string"}},
              "required": ["scheme_english"]}
    named = remote_gemma_chat(SCHEME_PROMPT.split("\n")[0], user, schema, 4096)["scheme_english"]
    if named.strip(" .").lower() not in ("", "none", "n/a"):
        result["scheme_english"] = named
    known = known_scheme(result["scheme_english"])
    if known:
        result["scheme"], result["scheme_english"] = known
    else:  # not on the list: keep the English name rather than garbled Kannada
        result["scheme"] = result["scheme_english"]

    # At the end of the long reply it tends to call a promise ("will be restarted") RESOLVED;
    # judged again on the English facts alone, in a short prompt, it gets these right.
    hints = outcome_phrases(result["atr_key_sentence"] or atr_text)
    user = (
        f"Request: {result['case_summary']}\n"
        f"ATR outcome sentence: {result['atr_key_sentence_english']}\n"
        f"What the office finally did: {result['final_action_taken']}\n"
        f"Same applicant: {result['same_applicant']}"
        + ("\nOutcome phrases found in the ATR (reliable):\n- " + "\n- ".join(hints) if hints else "")
        + "\n\nDone means already done; something that WILL be done later is not done yet."
    )
    schema = {"type": "object", "properties": {"reason": {"type": "string"},
                                               "verdict": VERDICT_SCHEMA["properties"]["verdict"]},
              "required": ["reason", "verdict"]}
    result.update(remote_gemma_chat(VERDICT_GUIDE, user, schema, 4096))
    return result


def check_case(case_id: str, citizen_text: str, atr_text: str, model: str = "gemma") -> dict:
    """Ask the model about one grievance; returns OUTPUT_FIELDS."""
    user_prompt = build_user_prompt(case_id, citizen_text, atr_text)
    if model == "gemini":
        result = ask_gemini(user_prompt)
    elif model == "qwen3":
        result = ask_gemma(user_prompt, ticked_items(citizen_text), qwen3_chat)
    elif model == "qwen14b":
        result = ask_gemma(user_prompt, ticked_items(citizen_text), qwen_chat)
    elif model == "gemma-e4b":
        result = ask_gemma_e4b(case_id, citizen_text, atr_text)
    else:
        result = ask_gemma(user_prompt, ticked_items(citizen_text))
    _, is_resolved, rating = VERDICT_INFO[result["verdict"]]
    result.update(case_id=case_id, is_resolved=is_resolved, rating=rating)
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


def finish_run(model: str):
    """Write observations.json and re-sync the workbooks after a run."""
    folders = observation_dirs(model)
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folders[0].glob("*Observation.json"))
    ]
    (folders[0] / "observations.json").write_text(
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


def main():
    parser = argparse.ArgumentParser(description="Check each ATR against the citizen's grievance.")
    parser.add_argument("--model", choices=MODELS, default="gemma", help="gemma (default), gemini, gemma-e4b, qwen14b or qwen3.")
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

    finish_run(args.model)


if __name__ == "__main__":
    main()
