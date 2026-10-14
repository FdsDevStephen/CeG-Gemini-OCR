using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using GrievanceApi.Exceptions;
using GrievanceApi.Interfaces;
using GrievanceApi.Options;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace GrievanceApi.Services;

/// <summary>
/// The remote Qwen3 14B server (port of qwen_chat / parse_qwen_reply in app/observation.py).
/// One plain prompt and no JSON mode; the reply's JSON object is cut out and checked.
/// </summary>
public sealed partial class QwenClient : ILlmClient
{
    readonly HttpClient http;
    readonly LlmOptions options;
    readonly ILogger<QwenClient> log;

    public QwenClient(HttpClient http, IOptions<LlmOptions> options, ILogger<QwenClient> log)
    {
        this.http = http;
        this.options = options.Value;
        this.log = log;
        http.Timeout = TimeSpan.FromSeconds(this.options.TimeoutSeconds);
    }

    public async Task<Dictionary<string, string>> ChatAsync(string system, string user, Field[] schema, CancellationToken ct)
    {
        var keys = string.Join(", ", schema.Select(f =>
            $"\"{f.Name}\"" + (f.Enum is null ? "" : $" (one of: {string.Join(", ", f.Enum)})")));
        // The prompts are raw strings in C#: keep their line breaks "\n", as in Python, whatever the file's line endings.
        var prompt = $"{system.ReplaceLineEndings("\n")}\n\n{user}\n\nReply with ONLY one JSON object, with exactly these string keys in this order: {keys}.";

        // The server drops connections and returns 500/504 now and then, and a reply can come
        // back without its JSON; a retry after a short wait usually works.
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                using var response = await http.PostAsJsonAsync(options.Url, new { prompt, max_tokens = options.MaxTokens }, ct);
                response.EnsureSuccessStatusCode();
                using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
                var text = json.RootElement.GetProperty("text").GetString() ?? "";
                return ParseReply(text, schema);
            }
            catch (Exception exc) when (exc is HttpRequestException or JsonException or InvalidDataException
                                        || exc is TaskCanceledException && !ct.IsCancellationRequested)
            {
                if (attempt >= options.Attempts)
                    throw new UpstreamException($"Qwen server at {options.Url} failed {attempt} times ({exc.Message})", exc);
                log.LogWarning("Qwen server attempt {Attempt} failed ({Error}); retrying", attempt, exc.Message);
                await Task.Delay(TimeSpan.FromSeconds(15 * attempt), ct);
            }
        }
    }

    static Dictionary<string, string> ParseReply(string text, Field[] schema)
    {
        var match = JsonObject().Match(text);
        if (!match.Success)
            throw new InvalidDataException($"No JSON in the model's reply (thinking may have used up max_tokens): {Tail(text, 200)}");

        using var json = JsonDocument.Parse(EscapeControlCharacters(match.Value));
        if (json.RootElement.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("The model's reply is not a JSON object");
        var result = new Dictionary<string, string>();
        foreach (var property in json.RootElement.EnumerateObject())
            result[property.Name] = property.Value.ValueKind switch
            {
                JsonValueKind.String => property.Value.GetString() ?? "",
                JsonValueKind.Null => "",
                _ => property.Value.GetRawText(),
            };

        var missing = schema.Where(f => !result.ContainsKey(f.Name)).Select(f => f.Name).ToList();
        if (missing.Count > 0)
            throw new InvalidDataException($"Reply is missing {string.Join(", ", missing)}");
        foreach (var field in schema.Where(f => f.Enum is not null))
            if (!field.Enum!.Contains(result[field.Name]))
                throw new InvalidDataException($"{field.Name} is not one of the labels: {result[field.Name]}");
        return result;
    }

    /// <summary>Raw line breaks and tabs inside JSON strings (Python's json.loads(strict=False) accepts them).</summary>
    static string EscapeControlCharacters(string json)
    {
        var output = new StringBuilder(json.Length);
        bool inString = false, escaped = false;
        foreach (var c in json)
        {
            if (inString && !escaped && c < ' ')
            {
                output.Append(c switch { '\n' => "\\n", '\r' => "\\r", '\t' => "\\t", _ => $"\\u{(int)c:x4}" });
                continue;
            }
            output.Append(c);
            if (escaped) escaped = false;
            else if (inString && c == '\\') escaped = true;
            else if (c == '"') inString = !inString;
        }
        return output.ToString();
    }

    static string Tail(string text, int length) => text.Length <= length ? text : text[^length..];

    [GeneratedRegex(@"\{.*\}", RegexOptions.Singleline)]
    private static partial Regex JsonObject();
}
