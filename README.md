# CeG Gemini OCR

OCR and grievance-checking tools for the PGRS (Public Grievance Redressal System).

1. **OCR**: reads every Citizen and ATR (Action Taken Report) document, PDF or image, with **Google Gemini**. If Gemini blocks a page, that page is read locally with **Tesseract** instead.
2. **Observation**: checks whether the ATR resolves the citizen's request, using the model you choose:

| Model | Option | Notes |
|---|---|---|
| Gemini 3.6 Flash | `gemini` (default) | Most accurate, paid API |
| Gemma 4 E2B | `gemma` | Free, runs locally on Ollama, ~30 s per case. Gives the same Resolved answer as Gemini on all 12 test cases. |

3. **Web UI**: upload one grievance folder and get its text and observation in the browser.

## Project Structure

```text
CeGOCR/
├── app/
│   ├── ocr.py            # Gemini OCR + Tesseract fallback; OCR the whole PGRS folder
│   ├── observation.py    # Check ATR vs grievance with Gemini or Gemma
│   └── excel.py          # Observations_<Model>.xlsx
├── streamlit_app.py      # Web UI
├── PGRS/                 # Input documents (not committed)
├── PGRS_Output/          # Extracted text (not committed)
├── Observation/          # Gemini/ and Gemma/: observations + Observations_<Model>.xlsx (not committed)
├── Folder and Txt/       # Shareable copy: PGRS/ with Extracted Text, and Observation/ (not committed)
├── .env
├── requirements.txt
└── README.md
```

## Requirements

* Python 3.10+
* A Google Gemini API key (OCR always uses Gemini)
* [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) on `PATH`, with English (`eng`) and Kannada (`kan`) language data
* For the `gemma` option only: [Ollama](https://ollama.com) running locally, with the model pulled:

```powershell
ollama pull gemma4:e2b
```

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=your_gemini_api_key_here

# Optional
GEMINI_INPUT_PRICE_PER_M=1.50     # USD per 1M input tokens
GEMINI_OUTPUT_PRICE_PER_M=7.50    # USD per 1M output + thinking tokens
TESSERACT_LANG=eng+kan
```

## Web UI

```powershell
streamlit run streamlit_app.py
```

Open `http://localhost:8501`, choose **Gemini** or **Gemma** in the sidebar, upload a grievance folder (one that contains `ATR` and `Citizen` subfolders), and click **Extract text and find observation**.

## Command Line

Input layout:

```text
PGRS/<grievance_id>/ATR/<pdf or image>
PGRS/<grievance_id>/Citizen/<pdf or image>
```

**1. OCR**

```powershell
python -m app.ocr                  # all grievances
python -m app.ocr --case 379425    # one grievance (repeat --case for more)
python -m app.ocr --force          # redo files that already have text
```

Writes `PGRS_Output/<id>/ATR.txt` and `Citizen.txt`, plus a copy in `Folder and Txt/PGRS/<id>/Extracted Text/`. `PGRS_Output/summary.json` records token usage and cost.

**2. Observation**

```powershell
python -m app.observation                  # all grievances, Gemini
python -m app.observation --model gemma    # all grievances, Gemma
python -m app.observation --case 379609 --force
```

Each model has its own folder, so their results never mix: Gemini writes `<id>Observation.json` and a row in `Observations_Gemini.xlsx` to `Observation/Gemini/`, Gemma writes to `Observation/Gemma/` and `Observations_Gemma.xlsx`. The same is written to `Folder and Txt/Observation/`. A copy of both workbooks is also kept directly in `Observation/` (not in `Folder and Txt/Observation/`). `Observation/<Model>/observations.json` lists every grievance.

```json
{
  "case_id": "379609",
  "is_resolved": false,
  "case_summary": "The citizen asked for a housing site to be allotted.",
  "final_action_taken": "No site was allotted; the citizen was told to apply to the Gram Panchayat and the request was closed.",
  "atr_key_sentence": "... ಕೋರಿಕೆ ಸಲ್ಲಿಸಲು ಎಂದು ತಿಳಿಯಪಡಿಸುತ್ತಾ ತಮ್ಮ ಉಲ್ಲೇಖಿತ ಮನವಿಯನ್ನು ವಿಲೇವಾರಿಗೊಳಿಸಲಾಗಿದೆ.",
  "atr_key_sentence_english": "... you are informed to submit a request to the concerned Gram Panchayat, and your request is hereby disposed of."
}
```

> **Accuracy:** OCR of Kannada official documents is not perfect. Have a person check the observations.

## Security

* Never put the API key in source code, and never commit `.env`.
* `PGRS/`, `PGRS_Output/`, `Observation/` and `Folder and Txt/` contain citizens' personal data and are excluded from git.
