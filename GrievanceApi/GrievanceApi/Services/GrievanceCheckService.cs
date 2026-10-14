using GrievanceApi.Exceptions;
using GrievanceApi.Helpers;
using GrievanceApi.Interfaces;
using GrievanceApi.Models;

namespace GrievanceApi.Services;

/// <summary>
/// The /api/grievances/check use case: a grievance folder (&lt;id&gt;/ATR/..., &lt;id&gt;/Citizen/...)
/// is OCR'd and checked; a single file is only OCR'd. A bad upload throws InvalidUploadException,
/// a failed OCR or LLM server UpstreamException.
/// </summary>
public sealed class GrievanceCheckService(IOcrService ocr, GrievanceChecker checker)
{
    public async Task<CheckResult> CheckAsync(string? caseId, IReadOnlyList<UploadedFile> files, CancellationToken ct)
    {
        if (files.Count == 0)
            throw new InvalidUploadException("Upload one file, or a grievance folder with ATR and Citizen subfolders.");
        var sided = files.Select(file => (File: file, Side: Uploads.SideOf(file.FileName))).ToList();

        // A single file: only OCR it.
        if (sided.All(f => f.Side is null))
        {
            if (files.Count > 1)
                throw new InvalidUploadException("Several files were uploaded, but none is inside an ATR or Citizen folder. "
                    + "Upload one file to OCR it, or a grievance folder (<id>/ATR/..., <id>/Citizen/...) to check it.");
            var file = files[0];
            if (!Uploads.IsSupported(file.FileName))
                throw new InvalidUploadException($"Only PDF and image files are supported: {Uploads.NameOf(file.FileName)}");
            return new CheckResult(null, new OcrResult(Uploads.NameOf(file.FileName), await ocr.OcrFilesAsync([file], ct)));
        }

        // A grievance folder: OCR its ATR and Citizen documents (other files are skipped, as in
        // run_folder.py), then check the grievance.
        var documents = sided.Where(f => f.Side is not null && Uploads.IsSupported(f.File.FileName)).ToList();
        List<UploadedFile> Side(string side) => documents.Where(f => f.Side!.Value.Side == side)
            .OrderBy(f => f.File.FileName, StringComparer.OrdinalIgnoreCase).Select(f => f.File).ToList();
        var atr = Side("ATR");
        var citizen = Side("Citizen");
        var missing = new[] { ("ATR", atr), ("Citizen", citizen) }.Where(s => s.Item2.Count == 0).Select(s => s.Item1).ToList();
        if (missing.Count > 0)
            throw new InvalidUploadException($"The folder has no PDF or image files in its {string.Join(" or ", missing)} folder.");
        var folders = documents.Select(f => f.Side!.Value.CaseFolder).OfType<string>().Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (folders.Count > 1)
            throw new InvalidUploadException($"Upload one grievance at a time; found folders {string.Join(", ", folders)}.");
        var id = string.IsNullOrWhiteSpace(caseId) ? folders.FirstOrDefault() : caseId.Trim();
        if (id is null)
            throw new InvalidUploadException("Send caseId, or upload the grievance's own folder (named with its case ID).");

        var atrText = await ocr.OcrFilesAsync(atr, ct);
        var citizenText = await ocr.OcrFilesAsync(citizen, ct);
        return new CheckResult(await checker.CheckAsync(id, citizenText, atrText, ct), null);
    }
}
