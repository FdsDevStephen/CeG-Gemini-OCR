namespace GrievanceApi.Options;

/// <summary>The Qwen3 14B server (same settings as app/observation.py). appsettings.json section "Llm".</summary>
public sealed class LlmOptions
{
    // generate_greedy: temperature 0 and no thinking, so the same text always gets the same verdict.
    public string Url { get; set; } = "http://101.53.137.13:8004/qwen13/generate_greedy";
    public int Attempts { get; set; } = 3;
    public int MaxTokens { get; set; } = 4096;
    public int TimeoutSeconds { get; set; } = 900;
}
