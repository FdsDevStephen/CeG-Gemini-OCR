using System.Diagnostics;
using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using GrievanceApi.Exceptions;
using GrievanceApi.Helpers;
using GrievanceApi.Interfaces;
using GrievanceApi.Models;
using GrievanceApi.Options;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using PdfSharp.Pdf.IO;

namespace GrievanceApi.Services;

/// <summary>
/// OCR through the hosted IndicOCR server (port of app/ocr_api.py, ocr_file_indic):
///     POST Url   multipart field "file"  ->  {"pages": [{"page_number", "markdown"}]}
/// Its markdown has "▁" where spaces belong, HTML tables and &lt;img&gt; placeholders;
/// CleanMarkdown turns that into plain text.
/// </summary>
public sealed partial class IndicOcrClient : IOcrService
{
    static readonly int[] RetryWaits = [10, 30, 60]; // seconds to wait before each retry when the server is down or busy

    readonly HttpClient http;
    readonly OcrOptions options;
    readonly ILogger<IndicOcrClient> log;

    public IndicOcrClient(HttpClient http, IOptions<OcrOptions> options, ILogger<IndicOcrClient> log)
    {
        this.http = http;
        this.options = options.Value;
        this.log = log;
        http.Timeout = TimeSpan.FromSeconds(this.options.TimeoutSeconds);
    }

    /// <summary>OCR several files into one text, each headed by its file name when there are several.</summary>
    public async Task<string> OcrFilesAsync(IReadOnlyList<UploadedFile> files, CancellationToken ct)
    {
        var texts = new List<string>();
        foreach (var file in files)
        {
            var name = Uploads.NameOf(file.FileName); // a folder's files arrive named with their path
            var text = await OcrFileAsync(name, file.Content, ct);
            texts.Add(files.Count > 1 ? $"===== {name} =====\n\n{text}" : text);
        }
        return string.Join("\n\n", texts);
    }

    public async Task<string> OcrFileAsync(string fileName, byte[] content, CancellationToken ct)
    {
        var pages = await PagesAsync(fileName, content, ct);
        var texts = pages.Select(page =>
        {
            var text = CleanMarkdown(page.Markdown);
            if (text.Length == 0)
                text = $"[OCR FAILED FOR PAGE {page.Number}]";
            return pages.Count > 1 ? $"===== Source page {page.Number} =====\n{text}" : text;
        });
        return string.Join("\n\n", texts);
    }

    /// <summary>Long PDFs go in small page ranges: each request stays short, and a failed range is retried on its own.</summary>
    async Task<List<Page>> PagesAsync(string fileName, byte[] content, CancellationToken ct)
    {
        int? total = null;
        if (Path.GetExtension(fileName).Equals(".pdf", StringComparison.OrdinalIgnoreCase) && options.ChunkPages > 0)
        {
            try
            {
                using var stream = new MemoryStream(content);
                total = PdfReader.Open(stream, PdfDocumentOpenMode.Import).PageCount;
            }
            catch (Exception)
            {
                total = null; // unreadable here -> let the server decide, whole file in one request
            }
        }
        if (total is null || total <= options.ChunkPages)
            return await RequestAsync(fileName, content, null, ct);

        var ranges = new List<string>();
        for (var start = 1; start <= total; start += options.ChunkPages)
            ranges.Add($"{start}-{Math.Min(start + options.ChunkPages - 1, total.Value)}");

        var chunks = new List<Page>[ranges.Count];
        await Parallel.ForEachAsync(
            Enumerable.Range(0, ranges.Count),
            new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, options.Parallel), CancellationToken = ct },
            async (i, token) => chunks[i] = await RequestAsync(fileName, content, ranges[i], token));
        return chunks.SelectMany(chunk => chunk).ToList();
    }

    /// <summary>One IndicOCR call (whole file, or only ?pages=&lt;range&gt;).</summary>
    async Task<List<Page>> RequestAsync(string fileName, byte[] content, string? pageRange, CancellationToken ct)
    {
        var url = pageRange is null ? options.Url : $"{options.Url}?pages={Uri.EscapeDataString(pageRange)}";
        var waits = RetryWaits.Cast<int?>().Append(null);
        foreach (var wait in waits)
        {
            string error;
            var timedOut = false;
            try
            {
                using var form = new MultipartFormDataContent();
                form.Add(new ByteArrayContent(content), "file", fileName);
                using var response = await http.PostAsync(url, form, ct);
                var body = await response.Content.ReadAsStringAsync(ct);
                if ((int)response.StatusCode < 500)
                {
                    if (response.StatusCode != HttpStatusCode.OK)
                        throw new UpstreamException($"IndicOCR server rejected {fileName}: HTTP {(int)response.StatusCode} {Truncate(body, 200)}");
                    return ParsePages(body);
                }
                error = $"HTTP {(int)response.StatusCode}: {Truncate(body, 100)}";
                timedOut = response.StatusCode == HttpStatusCode.GatewayTimeout;
            }
            catch (TaskCanceledException exc) when (!ct.IsCancellationRequested)
            {
                (error, timedOut) = (exc.Message, true);
            }
            catch (HttpRequestException exc)
            {
                error = exc.Message;
            }
            // A timeout means the document is too long for the server; retrying only wastes minutes.
            if (wait is null || timedOut)
                throw new UpstreamException($"OCR server failed on {fileName}: {error}");
            log.LogWarning("OCR server busy or unreachable ({Error}); retrying in {Wait}s", Truncate(error, 60), wait);
            await Task.Delay(TimeSpan.FromSeconds(wait.Value), ct);
        }
        throw new UnreachableException();
    }

    static List<Page> ParsePages(string body)
    {
        using var json = JsonDocument.Parse(body);
        return json.RootElement.GetProperty("pages").EnumerateArray()
            .Select(page => new Page(
                page.GetProperty("page_number").ToString(),
                page.GetProperty("markdown").GetString() ?? ""))
            .ToList();
    }

    /// <summary>IndicOCR markdown -> plain text: real spaces, one table row per line, no tags or photo labels.</summary>
    public static string CleanMarkdown(string markdown)
    {
        var text = markdown.Replace('▁', ' ');
        text = ImgTag().Replace(text, "");
        text = BrTag().Replace(text, " / ");
        text = CellBoundary().Replace(text, " | ");
        text = RowEnd().Replace(text, "\n");
        text = AnyTag().Replace(text, ""); // remaining table tags
        text = WebUtility.HtmlDecode(text);
        var lines = Text.SplitLines(text)
            .Select(line => Pipes().Replace(SpacesOrTabs().Replace(line, " "), " | ").Trim(' ', '|'));
        return ManyNewlines().Replace(string.Join("\n", lines), "\n\n").Trim();
    }

    static string Truncate(string text, int length) => text.Length <= length ? text : text[..length];

    sealed record Page(string Number, string Markdown);

    [GeneratedRegex(@"<img[^>]*>")] private static partial Regex ImgTag();
    [GeneratedRegex(@"<br\s*/?>")] private static partial Regex BrTag();
    [GeneratedRegex(@"</t[hd]>\s*<t[hd][^>]*>")] private static partial Regex CellBoundary();
    [GeneratedRegex(@"</tr>")] private static partial Regex RowEnd();
    [GeneratedRegex(@"<[^>]+>")] private static partial Regex AnyTag();
    [GeneratedRegex(@"[ \t]+")] private static partial Regex SpacesOrTabs();
    [GeneratedRegex(@"(\s*\|)+\s*")] private static partial Regex Pipes();
    [GeneratedRegex(@"\n{3,}")] private static partial Regex ManyNewlines();
}
