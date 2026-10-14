using System.Text.Json.Serialization;

namespace GrievanceApi.Models;

/// <summary>OCR text of one uploaded file (a single file is only OCR'd, not checked).</summary>
public sealed record OcrResult(
    [property: JsonPropertyName("file_name")] string FileName,
    [property: JsonPropertyName("text")] string Text);
