namespace GrievanceApi.Models;

/// <summary>
/// One uploaded file. FileName is as sent: a folder's files arrive named with their path inside
/// the folder, e.g. "448579/ATR/report.pdf".
/// </summary>
public sealed record UploadedFile(string FileName, byte[] Content);
