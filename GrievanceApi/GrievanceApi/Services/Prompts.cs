using GrievanceApi.Interfaces;

namespace GrievanceApi.Services;

/// <summary>The prompts and lookup tables of app/observation.py, word for word: keep the two in step.</summary>
static class Prompts
{
    // verdict -> (is_resolved, star rating of the service). Kept in code so every verdict always
    // maps to the same answers, whatever the model writes.
    // Rating: 3 = done; 2 = partly done, registered and being processed, sent to another office,
    // or more documents asked for; 1 = no action (pending, refused, closed, wrong action or unclear).
    public static readonly (string Verdict, bool IsResolved, int Rating)[] VerdictInfo =
    [
        ("RESOLVED", true, 3),
        ("PARTIALLY_RESOLVED", false, 2),
        ("IN_PROCESS", false, 2),
        ("PENDING", false, 1),
        ("REDIRECTED", false, 2),
        ("DOCUMENTS_REQUESTED", false, 2),
        ("REJECTED", false, 1),
        ("CLOSED_WITHOUT_ACTION", false, 1),
        ("WRONG_ACTION", false, 1),
        ("UNCLEAR", false, 1),
    ];

    // Field order matters: the model writes them in this order, so it names the scheme first,
    // and quotes and translates the ATR's decisive sentence before it is allowed to judge.
    public static readonly Field[] VerdictSchema =
    [
        new("scheme"),
        new("scheme_english"),
        new("case_summary"),
        new("atr_key_sentence"),
        new("atr_key_sentence_english"),
        new("final_action_taken"),
        new("same_applicant", ["YES", "NO", "UNCLEAR"]),
        new("verdict", VerdictInfo.Select(v => v.Verdict).ToArray()),
        new("reason"),
    ];

    // Options on the standard Praja Seva Andolana application form (Kannada keyword -> English),
    // so the ticked item reaches the model in English as well.
    public static readonly (string Kannada, string English)[] FormOptions =
    [
        ("ವಿಧವಾ", "widow pension"),
        ("ಅಂಗವಿಕಲ", "disability pension"),
        ("ಸಂಧ್ಯಾ", "Sandhya Suraksha old-age pension"),
        ("ಪಿಂಚಣಿ", "other pension"),
        ("ತಿದ್ದುಪಡಿ", "Pahani (RTC land record) correction"),
        ("ಪೋಡಿ", "Podi (land survey / subdivision)"),
        ("ಖಾತೆ", "Khata transfer"),
        ("ವಸತಿ", "housing scheme: site / house allotment"),
        ("ಪಡಿತರ", "new ration card"),
        ("ಸಾರಿಗೆ", "transport / KSRTC bus"),
        ("ರಸ್ತೆ", "road development"),
        ("ನೀರು", "drinking water"),
        ("ಸ್ಮಶಾನ", "cemetery development"),
        ("ದೇವಸ್ಥಾನ", "temple development / renovation"),
        ("ಶಾಲೆ", "government school / anganwadi development"),
        ("ನರೇಗಾ", "MGNREGA (job scheme)"),
        ("ಪೊಲೀಸ್", "police department service"),
        ("ಉದ್ಯೋಗ", "private employment"),
        ("ಕೆರೆ", "lake filling / irrigation"),
        ("ಗೃಹ ಲಕ್ಷ್ಮಿ", "Gruha Lakshmi scheme"),
        ("ಗೃಹ ಜ್ಯೋತಿ", "Gruha Jyothi (free electricity) scheme"),
        ("ಶಕ್ತಿ", "Shakti free bus scheme"),
        ("ಅನ್ನ", "Anna Bhagya (ration rice) scheme"),
        ("ಯುವ ನಿಧಿ", "Yuva Nidhi scheme"),
    ];

    // Outcome phrases looked up in the ATR in code, so the model is handed their English meaning
    // instead of having to read the Kannada (e.g. a negated "sanctioned").
    // \s* lets a phrase match when OCR broke the line in the middle of it.
    public static readonly (string Pattern, string Meaning)[] OutcomePhrases =
    [
        (@"ಮರು\s*ಚಾಲ್ತಿ", "the pension/benefit HAS BEEN RESTARTED (done)"),
        (@"ಮಂಜೂರಾತಿ\s*ನೀಡಿರುವುದಿಲ್ಲ|ಮಂಜೂರು\s*ಮಾಡಿರುವುದಿಲ್ಲ", "it has NOT been sanctioned"),
        (@"ಮಂಜೂರು\s*ಮಾಡಲಾಗಿ(?:ದೆ|ರುತ್ತದೆ)|ಮಂಜೂರಾತಿ\s*ನೀಡಲಾಗಿ(?:ದೆ|ರುತ್ತದೆ)", "it HAS BEEN sanctioned (done)"),
        (@"ಗುರಿ\s*ನಿಗದಿಪಡಿಸಿದ\s*ನಂತರ", "only AFTER the government fixes a target (future)"),
        (@"ಕ್ರಮ\s*ವಹಿಸಲಾಗು?ವುದು|ಕ್ರಮ\s*ಕೈಗೊಳ್ಳಲಾಗು?ವುದು", "action WILL be taken later (nothing done yet)"),
        (@"ಮುಕ್ತಾಯಗೊಳಿಸಲಾಗ", "the complaint is CLOSED"),
        (@"ವಿಲೇವಾರಿ\s*(?:ಗೊಳಿಸಲಾಗಿದೆ|ಮಾಡಲಾಗಿದೆ)", "the request has been DISPOSED of (closed)"),
        (@"ಲಗತ್ತಿಸಿರುವುದಿಲ್ಲ", "the citizen did NOT attach the required documents"),
        (@"ಸಲ್ಲಿಸಿದಲ್ಲಿ", "\"if you submit (the documents)\": the office ASKED for documents first (DOCUMENTS_REQUESTED)"),
        (@"ಖಾತೆಗೆ\s*(?:ಪಾವತಿ|ಜಮೆ)", "the money HAS BEEN PAID / credited to the citizen's account (done)"),
        (@"ಈಗಾಗಲೇ", "ALREADY: check what was already done - work done for the citizen, or only a notice "
                    + "telling the citizen to act (that is not done)"),
        (@"ಕರೆಯನ್ನು\s*ಸ್ವೀಕರಿಸಿರುವುದಿಲ್ಲ", "the citizen did NOT answer the office's phone call"),
        (@"ಸಂಬಂಧವಿರುವುದಿಲ್ಲ", "the applicant says they have NO connection to the application"),
        (@"ಅರ್ಹರಿರುವುದಿಲ್ಲ|ಅನರ್ಹ", "the citizen is NOT eligible"),
        (@"(?:ಪಂಚಾಯಿತಿ|ಕಛೇರಿ|ಇಲಾಖೆ)\S*\s*(?:ಕೋರಿಕೆ|ಅರ್ಜಿ|ಮನವಿ)\S*\s*ಸಲ್ಲಿಸ|ಸಲ್ಲಿಸುವಂತೆ\s*ಸೂಚಿಸ",
         "the citizen was TOLD TO APPLY to another office (e.g. the Gram Panchayat)"),
        (@"ಕಳುಹಿಸಲಾಗಿದೆ|ವರ್ಗಾಯಿಸಲಾಗಿದೆ|ರವಾನಿಸಲಾಗಿದೆ", "the request was FORWARDED to another office"),
        (@"ಪ್ರಗತಿಯಲ್ಲಿದೆ", "the work is IN PROGRESS"),
        (@"ತಂತ್ರಾಂಶದಲ್ಲಿ\s*(?:\S+\s*){0,3}?(?:ಅಳವಡಿಸಲಾಗಿ|ನೋಂದಾಯಿಸಲಾಗಿ|ನೋಂದಣಿ)",
         "the request was ENTERED / REGISTERED in the software: taken up, NOT fulfilled yet (IN_PROCESS)"),
        (@"ಕ್ರಮ\s*(?:ವಹಿಸಲಾಗುತ್ತದೆ|ಕೈಗೊಳ್ಳಲಾಗುತ್ತದೆ)", "action WILL be taken (future; nothing done yet)"),
        (@"ತಡೆ\s*ಹಿಡಿಯಲಾಗಿ", "the pension/benefit is WITHHELD / on hold (NOT restarted)"),
        (@"ಸ್ಥಗಿತಗೊಂಡಿರುವುದರಿಂದ", "\"because it had stopped\" (background only, NOT the outcome)"),
    ];

    public const string Translate = """
You translate Kannada sentences from Karnataka government Action Taken Reports into English.
Translate faithfully and completely: keep who did what, what was or was not done, and any date.
Do not summarise, do not add facts, and write English only - no Kannada script.
Verb endings: ...ಲಾಗಿದೆ / ...ಲಾಗಿರುತ್ತದೆ = has been done; ...ಲಾಗುವುದು = will be done;
...ಇರುವುದಿಲ್ಲ = has not been done.
Words: ಗ್ರಾಮ ಸಭೆ = Grama Sabha (village assembly); ಗ್ರಾಮ ಪಂಚಾಯಿತಿ = Gram Panchayat;
ಕೋರಿಕೆ ಸಲ್ಲಿಸಲು = to submit a request; ತಿಳಿಯಪಡಿಸಿದೆ = you are informed; ನಿವೇಶನ = site / plot;
ಪಿಂಚಣಿ ಸೌಲಭ್ಯ = pension benefit; ಮರು ಚಾಲ್ತಿ = restarted.
""";

    public const string Scheme = """
You name the one Karnataka government scheme or service a citizen's grievance is about.
Answer with exactly ONE scheme, never a list.
scheme: its name in Kannada script. scheme_english: the same name in English.
If the citizen ticked a scheme on the form, that is the scheme. If nothing was ticked, the
form's printed options are NOT the request: base it on the Request and Outcome lines.
If the request is not a scheme (e.g. a road, drainage, a loan, a quarry lease), name that
service in Kannada.
""";

    public const string Transliterate = """
Write the given Karnataka government scheme or service name in Kannada script, as it is
officially written in Kannada. Keep the same scheme; do not replace it with another one.
""";

    public const string System = """
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
""";
}
