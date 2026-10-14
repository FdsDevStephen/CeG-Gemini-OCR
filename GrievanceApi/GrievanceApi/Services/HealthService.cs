using GrievanceApi.Interfaces;
using GrievanceApi.Models;

namespace GrievanceApi.Services;

/// <summary>The /health use case: the API is up, and are the OCR and LLM servers answering?</summary>
public sealed class HealthService(IExternalServicesProbe probe)
{
    public async Task<HealthReport> CheckAsync(CancellationToken ct)
    {
        var ocrServer = await probe.ProbeOcrAsync(ct);
        var llmServer = await probe.ProbeLlmAsync(ct);
        var healthy = ocrServer.Up && llmServer.Up;
        return new HealthReport(healthy ? "Healthy" : "Unhealthy", "Up", ocrServer, llmServer);
    }
}
