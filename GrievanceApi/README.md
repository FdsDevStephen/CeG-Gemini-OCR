# Grievance Check API

Upload a grievance's documents; the API OCRs them on the IndicOCR server and asks the Qwen3 14B
server whether the government's Action Taken Report (ATR) resolved the citizen's grievance. It
gives the same answers as the Python pipeline (`run_folder.py` / `observe_text.py`).

## Endpoints

| Method | Route | Request | Response |
|---|---|---|---|
| POST | `/api/grievances/check` | `multipart/form-data`: `files`, `caseId` (optional) | 200 observation, or OCR text for a single file; 400 `{"error"}`; 502 ProblemDetails |
| GET | `/health` | – | 200 or 503 `{"status","api","ocrServer","llmServer"}` |

- **A grievance folder:** send its files named with their path inside the folder,
  `<id>/ATR/...` and `<id>/Citizen/...` (as a browser folder picker does). You get the
  observation: `case_id, is_resolved, rating, scheme, scheme_english, case_summary,
  final_action_taken, atr_key_sentence, atr_key_sentence_english`. The case ID comes from the
  folder name unless `caseId` is sent.
- **A single file:** you get only its OCR text, `{"file_name", "text"}`.
- OCR takes ~30-45 s per page, so a check can take several minutes.
- `/health` only checks that both servers answer an HTTP request (well under a second); it OCRs
  nothing. A server that answers with an error (e.g. 503) still counts as "up".

Test page (folder or file upload): `/`. Swagger: `/swagger` (single files only).

## Structure

One ASP.NET Core Web API project:

```
GrievanceApi/
├── GrievanceApi.slnx
└── GrievanceApi/
    ├── Controllers/
    │   ├── GrievancesController.cs   POST /api/grievances/check
    │   └── HealthController.cs       GET /health
    ├── Services/
    │   ├── GrievanceCheckService.cs  the upload: sorts files into ATR / Citizen, validates, OCR, check
    │   ├── GrievanceChecker.cs       the verdict: builds the prompt, asks the LLM, follow-ups, rating
    │   ├── Prompts.cs                the prompts and lookup tables (word for word from app/observation.py)
    │   ├── IndicOcrClient.cs         the OCR server: 2-page chunks, retries, cleaning the markdown
    │   ├── QwenClient.cs             the LLM server: prompt format, JSON checks, retries
    │   ├── ExternalServicesProbe.cs  does each server answer? (for /health)
    │   └── HealthService.cs          builds the /health answer
    ├── Interfaces/                   IOcrService, ILlmClient (+ Field), IExternalServicesProbe
    ├── Models/                       Observation, OcrResult, UploadedFile, CheckResult, HealthReport,
    │                                 CheckGrievanceRequest (the upload form)
    ├── Options/                      OcrOptions, LlmOptions (appsettings sections "Ocr" and "Llm")
    ├── Exceptions/                   InvalidUploadException (400), UpstreamException (502)
    ├── Middleware/                   ApiExceptionHandler: those exceptions -> 400 / 502
    ├── Helpers/                      Uploads (file types, folder paths), Text, AllFormFilesAttribute
    ├── wwwroot/index.html            the upload test page
    ├── Properties/launchSettings.json   https://localhost:7317 (+ http://localhost:5317), opens /swagger
    ├── appsettings.json              server URLs, chunk size, timeouts
    ├── GrievanceApi.http             requests to send from Visual Studio
    └── Program.cs                    service registrations and the request pipeline
```

## How a request flows

```
POST /api/grievances/check
  GrievancesController            binds the form, turns each IFormFile into an UploadedFile
  -> GrievanceCheckService        sorts the files into ATR / Citizen (Helpers/Uploads), validates
     -> IOcrService               IndicOcrClient: OCR server
     -> GrievanceChecker          builds the prompt, -> ILlmClient (QwenClient: LLM server), verdict -> rating
  <- 200 observation
  A bad upload throws InvalidUploadException and a server failure UpstreamException;
  ApiExceptionHandler turns them into 400 {"error"} and 502 ProblemDetails.

GET /health
  HealthController -> HealthService -> IExternalServicesProbe (ExternalServicesProbe) -> 200 / 503
```

## Run

**Visual Studio 2026:** open `GrievanceApi.slnx`, pick the **https** profile and press
**Ctrl+F5**. Swagger opens at https://localhost:7317/swagger. (With F5, Visual Studio stops the
API when you close its browser window.)

**Command line** (`dotnet` may not be on PATH):

```
"C:\Program Files\dotnet\dotnet.exe" run --project GrievanceApi --launch-profile https
```

The first HTTPS run needs the trusted development certificate:
`"C:\Program Files\dotnet\dotnet.exe" dev-certs https --trust`.

## Keep in step with the Python pipeline

`Services/Prompts.cs` and `Services/GrievanceChecker.cs` are a word-for-word port of
`app/observation.py`, and `Services/IndicOcrClient.cs` of `app/ocr_api.py`. A prompt change in
one must be made in the other, or the API's verdicts will drift from the Excel results.
