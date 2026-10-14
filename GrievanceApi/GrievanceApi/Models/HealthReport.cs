using System.Text.Json.Serialization;

namespace GrievanceApi.Models;

/// <summary>Whether one server answered, how long it took, and the error when it did not.</summary>
public sealed record ServerStatus(bool Up, long Milliseconds, string? Error);

/// <summary>The /health answer.</summary>
public sealed record HealthReport(string Status, string Api, ServerStatus OcrServer, ServerStatus LlmServer)
{
    [JsonIgnore] // not part of the JSON; the API answers 200 or 503 from it
    public bool IsHealthy => OcrServer.Up && LlmServer.Up;
}
