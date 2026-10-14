namespace GrievanceApi.Interfaces;

/// <summary>One field the model must fill in; Enum lists its allowed labels.</summary>
public sealed record Field(string Name, string[]? Enum = null);

/// <summary>The LLM server: one prompt in, one checked JSON object (field name -> value) out.</summary>
public interface ILlmClient
{
    Task<Dictionary<string, string>> ChatAsync(string system, string user, Field[] schema, CancellationToken ct);
}
