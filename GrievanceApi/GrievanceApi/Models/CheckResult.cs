namespace GrievanceApi.Models;

/// <summary>What a check returns: the observation for a grievance folder, or the OCR text of a single file.</summary>
public sealed record CheckResult(Observation? Observation, OcrResult? OcrText);
