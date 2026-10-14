using System.Diagnostics;
using GrievanceApi.Interfaces;
using GrievanceApi.Models;
using GrievanceApi.Options;
using Microsoft.Extensions.Options;

namespace GrievanceApi.Services;

/// <summary>
/// Any HTTP answer (these endpoints answer a GET with 405) means the server is up; only no answer is down.
/// Nothing is OCR'd or generated, so the check takes well under a second.
/// </summary>
public sealed class ExternalServicesProbe(IHttpClientFactory factory, IOptions<OcrOptions> ocr, IOptions<LlmOptions> llm)
    : IExternalServicesProbe
{
    public const string HttpClientName = "health"; // 5 s timeout, set in AddInfrastructure

    public Task<ServerStatus> ProbeOcrAsync(CancellationToken ct) => Reachable(factory.CreateClient(HttpClientName), ocr.Value.Url, ct);

    public Task<ServerStatus> ProbeLlmAsync(CancellationToken ct) => Reachable(factory.CreateClient(HttpClientName), llm.Value.Url, ct);

    static async Task<ServerStatus> Reachable(HttpClient http, string url, CancellationToken ct)
    {
        var timer = Stopwatch.StartNew();
        try
        {
            using var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct);
            return new ServerStatus(true, timer.ElapsedMilliseconds, null);
        }
        catch (Exception exc) when (exc is HttpRequestException || exc is TaskCanceledException && !ct.IsCancellationRequested)
        {
            return new ServerStatus(false, timer.ElapsedMilliseconds, exc.Message);
        }
    }
}
