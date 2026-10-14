using GrievanceApi.Interfaces;
using GrievanceApi.Middleware;
using GrievanceApi.Options;
using GrievanceApi.Services;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.OpenApi;

// Grievance check API: OCR the ATR and Citizen documents on the IndicOCR server, then ask the
// Qwen3 14B server whether the ATR resolves the grievance (the same steps as run_folder.py).
//
//   POST /api/grievances/check   multipart/form-data "files": a grievance folder's files (<id>/ATR/..., <id>/Citizen/...)
//                                -> 200 the observation; or one file -> 200 its OCR text only.
//                                400 bad request, 502 the OCR or LLM server failed.
//                                OCR takes ~30-45 s per page, so a call can take several minutes.
//   GET  /health                 200 if the API and both servers answer, else 503.
//
// Test page (folder or file upload): /     Swagger: /swagger

const long MaxUploadBytes = 100 * 1024 * 1024;

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(kestrel => kestrel.Limits.MaxRequestBodySize = MaxUploadBytes);
builder.Services.Configure<FormOptions>(form => form.MultipartBodyLengthLimit = MaxUploadBytes);

builder.Services.AddControllers();
builder.Services.Configure<ApiBehaviorOptions>(api =>
{
    // The controllers check their own input and answer 400 {"error": ...} (via ApiExceptionHandler);
    // don't let [ApiController] answer first with its own validation format,
    api.SuppressModelStateInvalidFilter = true;
    // and keep a bare 415 (wrong content type) without a ProblemDetails body.
    api.SuppressMapClientErrors = true;
});

// Settings (appsettings.json sections "Ocr" and "Llm").
builder.Services.Configure<OcrOptions>(builder.Configuration.GetSection("Ocr"));
builder.Services.Configure<LlmOptions>(builder.Configuration.GetSection("Llm"));

// The OCR and LLM servers.
builder.Services.AddHttpClient<IOcrService, IndicOcrClient>()
    .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { ConnectTimeout = TimeSpan.FromSeconds(30) });
builder.Services.AddHttpClient<ILlmClient, QwenClient>()
    .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { ConnectTimeout = TimeSpan.FromSeconds(30) });
builder.Services.AddHttpClient(ExternalServicesProbe.HttpClientName, http => http.Timeout = TimeSpan.FromSeconds(5));
builder.Services.AddTransient<IExternalServicesProbe, ExternalServicesProbe>();

// The services the controllers call.
builder.Services.AddTransient<GrievanceChecker>();
builder.Services.AddTransient<GrievanceCheckService>();
builder.Services.AddTransient<HealthService>();

// Errors: InvalidUploadException -> 400, UpstreamException -> 502.
builder.Services.AddExceptionHandler<ApiExceptionHandler>();
builder.Services.AddProblemDetails();

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(swagger => swagger.SwaggerDoc("v1", new OpenApiInfo
{
    Title = "Grievance Check API",
    Version = "v1",
    Description = "Upload a grievance's ATR and Citizen documents; the API OCRs them and decides whether the ATR "
        + "resolves the grievance. OCR takes ~30-45 s per page, so a check can take several minutes.",
}));

var app = builder.Build();
app.UseExceptionHandler(); // InvalidUploadException -> 400, UpstreamException -> 502 (ApiExceptionHandler)
app.UseHttpsRedirection(); // http:// requests are sent on to https:// (when the app listens on https)
app.UseSwagger();
app.UseSwaggerUI(ui => ui.DocumentTitle = "Grievance Check API");
app.UseDefaultFiles(); // "/" -> wwwroot/index.html, the upload test page
app.UseStaticFiles();
app.MapControllers();

app.Run();
