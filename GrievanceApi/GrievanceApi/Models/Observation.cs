using System.Text.Json.Serialization;

namespace GrievanceApi.Models;

/// <summary>The observation for one grievance: the same fields as the Excel row / &lt;id&gt;Observation.json.</summary>
public sealed record Observation(
    [property: JsonPropertyName("case_id")] string CaseId,
    [property: JsonPropertyName("is_resolved")] bool IsResolved,
    [property: JsonPropertyName("rating")] int Rating,
    [property: JsonPropertyName("scheme")] string Scheme,
    [property: JsonPropertyName("scheme_english")] string SchemeEnglish,
    [property: JsonPropertyName("case_summary")] string CaseSummary,
    [property: JsonPropertyName("final_action_taken")] string FinalActionTaken,
    [property: JsonPropertyName("atr_key_sentence")] string AtrKeySentence,
    [property: JsonPropertyName("atr_key_sentence_english")] string AtrKeySentenceEnglish);
