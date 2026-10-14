namespace GrievanceApi.Helpers;

/// <summary>
/// Sorting the uploaded files. A folder arrives as its files, each named with its path inside the
/// folder (as a browser's folder picker sends them), e.g. "448579/ATR/report.pdf":
/// the first folder named ATR or Citizen says which side a file is, and the folder above it is the
/// grievance (case ID), as in run_folder.py's &lt;folder&gt;\&lt;id&gt;\ATR\..., Citizen\...
/// </summary>
public static class Uploads
{
    public static readonly string[] Sides = ["ATR", "Citizen"];

    /// <summary>The document types the OCR server reads.</summary>
    public static readonly HashSet<string> SupportedExtensions =
        [".pdf", ".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp"];

    static string[] PathParts(string fileName) =>
        fileName.Split(['/', '\\'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    /// <summary>The file's own name, without the folders in front of it.</summary>
    public static string NameOf(string fileName) => PathParts(fileName).LastOrDefault() ?? fileName;

    public static bool IsSupported(string fileName) =>
        SupportedExtensions.Contains(Path.GetExtension(NameOf(fileName)).ToLowerInvariant());

    /// <summary>"ATR" or "Citizen" and the grievance folder above it, or null if the file is in neither.</summary>
    public static (string Side, string? CaseFolder)? SideOf(string fileName)
    {
        var parts = PathParts(fileName);
        for (var i = 0; i < parts.Length - 1; i++) // the last part is the file name
            foreach (var side in Sides)
                if (parts[i].Equals(side, StringComparison.OrdinalIgnoreCase))
                    return (side, i > 0 ? parts[i - 1] : null);
        return null;
    }
}
