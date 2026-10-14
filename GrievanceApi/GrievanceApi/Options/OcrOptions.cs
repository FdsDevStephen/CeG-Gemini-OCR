namespace GrievanceApi.Options;

/// <summary>The IndicOCR server (same settings as app/ocr_api.py). appsettings.json section "Ocr".</summary>
public sealed class OcrOptions
{
    // No trailing slash: that redirects to 127.0.0.1.
    public string Url { get; set; } = "http://101.53.137.13:8004/indicocr/ocr_markdown";
    public int ChunkPages { get; set; } = 2;      // pages per request for a PDF (0 = whole file in one request)
    public int Parallel { get; set; } = 1;        // chunks sent at the same time
    public int TimeoutSeconds { get; set; } = 3600; // IndicOCR needs ~30-45 s per page and may wait in the server queue
}
