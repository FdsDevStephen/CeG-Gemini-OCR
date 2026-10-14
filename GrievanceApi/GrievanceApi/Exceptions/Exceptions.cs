namespace GrievanceApi.Exceptions;

/// <summary>The OCR or LLM server failed; the API answers 502.</summary>
public sealed class UpstreamException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>The upload cannot be checked (missing side, unsupported file, ...); the API answers 400 {"error": message}.</summary>
public sealed class InvalidUploadException(string message) : Exception(message);
