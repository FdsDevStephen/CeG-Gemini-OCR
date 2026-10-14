using System.ComponentModel.DataAnnotations;
using GrievanceApi.Helpers;
using Microsoft.AspNetCore.Mvc;

namespace GrievanceApi.Models;

/// <summary>The /api/grievances/check form.</summary>
public sealed class CheckGrievanceRequest
{
    /// <summary>The grievance number; optional, defaults to the uploaded folder's name.</summary>
    [FromForm(Name = "caseId")]
    public string? CaseId { get; set; }

    /// <summary>A grievance folder's files (named with their paths), or one file. Any field name is accepted.</summary>
    // [Required] only marks the field as required in Swagger: automatic validation is off, so a missing
    // file still gets the service's own 400 message.
    [Required]
    [AllFormFiles(Name = "files")]
    public IFormFileCollection? Files { get; set; }

    /// <summary>The uploads as plain records, so the inner layers never see ASP.NET's IFormFile.</summary>
    public async Task<List<UploadedFile>> ToUploadedFilesAsync(CancellationToken ct)
    {
        var files = new List<UploadedFile>();
        foreach (var file in Files ?? (IEnumerable<IFormFile>)[])
        {
            using var buffer = new MemoryStream();
            await file.CopyToAsync(buffer, ct);
            files.Add(new UploadedFile(file.FileName, buffer.ToArray()));
        }
        return files;
    }
}
