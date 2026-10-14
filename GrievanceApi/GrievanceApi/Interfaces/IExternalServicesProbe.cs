using GrievanceApi.Models;

namespace GrievanceApi.Interfaces;

/// <summary>Whether the OCR and LLM servers answer at all (nothing is OCR'd or generated).</summary>
public interface IExternalServicesProbe
{
    Task<ServerStatus> ProbeOcrAsync(CancellationToken ct);
    Task<ServerStatus> ProbeLlmAsync(CancellationToken ct);
}
