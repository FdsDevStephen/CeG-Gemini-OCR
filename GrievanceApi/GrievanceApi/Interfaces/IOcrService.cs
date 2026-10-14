using GrievanceApi.Models;

namespace GrievanceApi.Interfaces;

/// <summary>The OCR server.</summary>
public interface IOcrService
{
    /// <summary>OCR several files into one text, each headed by its file name when there are several.</summary>
    Task<string> OcrFilesAsync(IReadOnlyList<UploadedFile> files, CancellationToken ct);
}
