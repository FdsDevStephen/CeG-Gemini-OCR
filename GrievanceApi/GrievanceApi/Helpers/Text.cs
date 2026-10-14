using System.Text.RegularExpressions;

namespace GrievanceApi.Helpers;

public static class Text
{
    // Every line break Python's str.splitlines() knows; the last two are the Unicode line and paragraph separators.
    static readonly Regex LineBreak = new(
        "\r\n|[\n\r\v\f\x1c\x1d\x1e\x85" + (char)0x2028 + (char)0x2029 + "]", RegexOptions.Compiled);

    /// <summary>Like Python's str.splitlines() (no empty last line after a final line break).</summary>
    public static List<string> SplitLines(string text)
    {
        var lines = LineBreak.Split(text).ToList();
        if (lines.Count > 0 && lines[^1].Length == 0)
            lines.RemoveAt(lines.Count - 1);
        return lines;
    }
}
