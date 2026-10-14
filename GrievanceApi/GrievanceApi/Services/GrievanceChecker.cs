using System.Text.RegularExpressions;
using GrievanceApi.Helpers;
using GrievanceApi.Interfaces;
using GrievanceApi.Models;

namespace GrievanceApi.Services;

/// <summary>
/// Decides whether the ATR resolves the citizen's grievance (port of check_case with
/// model "qwen14b" in app/observation.py: build_user_prompt, then ask_gemma with qwen_chat).
/// </summary>
public sealed partial class GrievanceChecker(ILlmClient llm)
{
    public async Task<Observation> CheckAsync(string caseId, string citizenText, string atrText, CancellationToken ct)
    {
        var userPrompt = BuildUserPrompt(caseId, citizenText, atrText);
        var result = await AskAsync(userPrompt, TickedItems(citizenText), ct);
        var (_, isResolved, rating) = Prompts.VerdictInfo.First(v => v.Verdict == result["verdict"]);
        return new Observation(
            caseId, isResolved, rating,
            result["scheme"], result["scheme_english"], result["case_summary"], result["final_action_taken"],
            result["atr_key_sentence"], result["atr_key_sentence_english"]);
    }

    async Task<Dictionary<string, string>> AskAsync(string userPrompt, List<string> ticked, CancellationToken ct)
    {
        var result = await llm.ChatAsync(Prompts.System, userPrompt, Prompts.VerdictSchema, ct);

        // The model sometimes copies (or romanises) the Kannada instead of translating it;
        // then translate that one sentence on its own. Only as a fallback: on sentences it
        // already got right, this tends to invent details.
        var english = result["atr_key_sentence_english"];
        if (Kannada().IsMatch(english) || !EnglishWord().IsMatch(english))
        {
            var hints = OutcomePhrases(result["atr_key_sentence"]);
            var user = result["atr_key_sentence"]
                + (hints.Count > 0 ? "\n\nKnown phrase meanings:\n- " + string.Join("\n- ", hints) : "");
            result["atr_key_sentence_english"] =
                (await llm.ChatAsync(Prompts.Translate, user, [new("english")], ct))["english"];
        }

        // It also tends to list every scheme on the form, or write the scheme in English;
        // then ask for the one scheme on its own.
        if (!Kannada().IsMatch(result["scheme"]) || SchemeList().IsMatch(result["scheme"]))
            await FixSchemeAsync(result, ticked, ct);
        return result;
    }

    /// <summary>Write a single English scheme name in Kannada, or pick one scheme from a list.</summary>
    async Task FixSchemeAsync(Dictionary<string, string> result, List<string> ticked, CancellationToken ct)
    {
        if (!SchemeList().IsMatch(result["scheme"]))
        {
            result["scheme"] = (await llm.ChatAsync(Prompts.Transliterate, result["scheme"], [new("scheme")], ct))["scheme"];
            return;
        }
        var user =
            $"Request: {result["case_summary"]}\n"
            + $"Outcome: {result["final_action_taken"]}\n"
            + $"Ticked on the citizen's form: {(ticked.Count > 0 ? string.Join("; ", ticked) : "nothing detected")}\n"
            + $"Scheme names seen in the documents (may be the form's whole list of options): {result["scheme"]}";
        var answer = await llm.ChatAsync(Prompts.Scheme, user, [new("scheme"), new("scheme_english")], ct);
        result["scheme"] = answer["scheme"];
        result["scheme_english"] = answer["scheme_english"];
    }

    public static string BuildUserPrompt(string caseId, string citizenText, string atrText)
    {
        var ticked = TickedItems(citizenText);
        var notes = new List<string>
        {
            ticked.Count > 0
                ? "Ticked on the citizen's form: " + string.Join("; ", ticked)
                : "No ticked form option was detected; rely on the citizen's letter.",
        };
        var phrases = OutcomePhrases(atrText);
        if (phrases.Count > 0)
            notes.Add("Outcome phrases found in the ATR (reliable translations):\n- " + string.Join("\n- ", phrases));
        var entry = ApplicantEntry(caseId, citizenText, atrText);
        if (entry is not null)
            notes.Add(
                "The ATR answers many applicants in one list. This citizen's own entry in that list "
                + $"(serial number, grievance number, name, ..., the office's remark):\n{entry}\n"
                + "Decide the verdict from THIS entry's serial number and remark, using what the ATR "
                + "says for that range of serial numbers - not from what was done for other applicants.");
        var others = entry is null ? OtherGrievanceNumbers(caseId, atrText) : [];
        if (others.Count > 0)
            notes.Add(
                $"This case is grievance {caseId}, but the ATR cites grievance number(s) "
                + $"{string.Join(", ", others)}: it is probably a reply to a different grievance.");
        return "=== CITIZEN DOCUMENT ===\n"
            + $"{citizenText.Trim()}\n\n"
            + "=== ATR (ACTION TAKEN REPORT) ===\n"
            + $"{atrText.Trim()}\n\n"
            + "=== CHECKS DONE IN CODE ===\n"
            + string.Join("\n", notes)
            + "\n\nReturn the JSON verdict.";
    }

    /// <summary>Numbered form options the citizen ticked, each with its English meaning when known.</summary>
    public static List<string> TickedItems(string citizenText)
    {
        var items = new List<string>();
        foreach (var line in Text.SplitLines(citizenText))
        {
            // Table cells, and options stacked in one cell with <br>; drop the "(✓ ನಮೂದಿಸಿ)" hints.
            var segments = CellSplit().Split(Parenthesised().Replace(line, "")).Select(x => x.Trim(' ', '*')).ToList();
            for (var i = 0; i < segments.Count; i++)
            {
                if (!Tick().IsMatch(segments[i]))
                    continue;
                var option = Tick().Replace(segments[i], "").Trim(' ', '*');
                if (option.Length == 0)
                    option = segments.Take(i).Reverse().FirstOrDefault(x => x.Length > 0 && !EmptyBox().IsMatch(x)) ?? "";
                if (!OptionNumber().IsMatch(option))
                    continue;
                var english = Prompts.FormOptions.FirstOrDefault(o => option.Contains(o.Kannada)).English;
                items.Add(english is not null ? $"{option} ({english})" : option);
            }
        }
        return items;
    }

    /// <summary>English meaning of each known outcome phrase found in the ATR, in reading order.</summary>
    public static List<string> OutcomePhrases(string atrText)
    {
        var found = new List<(int Start, string Text)>();
        foreach (var (pattern, meaning) in Prompts.OutcomePhrases)
        {
            var match = Regex.Match(atrText, pattern);
            if (match.Success)
                found.Add((match.Index, $"{string.Join(' ', SplitWords(match.Value))} = {meaning}"));
        }
        return found.OrderBy(f => f.Start).Select(f => f.Text).ToList();
    }

    /// <summary>Grievance numbers the ATR cites, if none of them is this case's number.</summary>
    static List<string> OtherGrievanceNumbers(string caseId, string atrText)
    {
        var numbers = GrievanceNumber().Matches(atrText).Select(m => m.Groups[1].Value)
            .Distinct().Order(StringComparer.Ordinal).ToList();
        return numbers.Count == 0 || numbers.Contains(caseId) ? [] : numbers;
    }

    /// <summary>Every markdown table in the OCR text, as rows of cells (the --- separator row dropped).</summary>
    static List<List<List<string>>> OcrTables(string text)
    {
        var tables = new List<List<List<string>>>();
        var rows = new List<List<string>>();
        foreach (var line in Text.SplitLines(text).Append(""))
        {
            // Gemini writes "| a | b |"; Indic-OCR writes "a | b | c".
            if (line.Trim().StartsWith('|') || CountOf(line, " | ") >= 2)
            {
                var cells = line.Trim().Trim('|').Split('|').Select(c => c.Trim()).ToList();
                if (!cells.All(c => SeparatorCell().IsMatch(c)))
                    rows.Add(cells);
            }
            else if (rows.Count > 0)
            {
                tables.Add(rows);
                rows = [];
            }
        }
        return tables;
    }

    /// <summary>
    /// This citizen's own entry in an ATR that answers many applicants in one list. The list is
    /// found by grievance number; OCR often gets one digit wrong, so a number one digit off also
    /// counts when the entry's name appears in the citizen's document. Each applicant can be a
    /// row, or (as OCR often lays it out) a column.
    /// </summary>
    static string? ApplicantEntry(string caseId, string citizenText, string atrText)
    {
        var citizenWords = ThreeOrMoreChars().Matches(citizenText).Select(m => m.Value).ToHashSet();
        foreach (var table in OcrTables(atrText))
        {
            var width = table.Max(row => row.Count);
            var rows = table.Select(row => row.Concat(Enumerable.Repeat("", width - row.Count)).ToList()).ToList();
            var columnsAreApplicants = rows.Any(row => row.Count(c => SixDigits().IsMatch(c)) >= 2);
            var entries = columnsAreApplicants
                ? Enumerable.Range(0, width).Select(col => rows.Select(row => row[col]).ToList()).ToList()
                : rows;
            if (entries.Count < 3)
                continue;
            var exact = entries.Where(e => e.Contains(caseId)).ToList();
            var near = entries.Where(e =>
                e.Any(c => OneDigitApart(caseId, c))
                && e.Where(c => !IsAllDigits(c)).SelectMany(SplitWords).Any(w => w.Length >= 3 && citizenWords.Contains(w))).ToList();
            var found = exact.Count > 0 ? exact : near;
            if (found.Count == 1)
                return string.Join(" | ", found[0].Where(c => c.Length > 0));
        }
        return null;
    }

    static bool OneDigitApart(string a, string b) => a.Length == b.Length && a.Zip(b).Count(p => p.First != p.Second) == 1;

    static bool IsAllDigits(string text) => text.Length > 0 && text.All(char.IsDigit);

    static string[] SplitWords(string text) => text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);

    static int CountOf(string text, string part)
    {
        int count = 0, index = 0;
        while ((index = text.IndexOf(part, index, StringComparison.Ordinal)) >= 0)
        {
            count++;
            index += part.Length;
        }
        return count;
    }

    [GeneratedRegex(@"\[\s*[✓✔☑xX]\s*\]|[✓✔☑]")] private static partial Regex Tick();
    [GeneratedRegex(@"^\[\s*\]\z")] private static partial Regex EmptyBox();
    [GeneratedRegex(@"^(\d+|[IVXL]+)\s*[.)]")] private static partial Regex OptionNumber(); // "8. ..." or "IV. ..."
    [GeneratedRegex(@"\||<br>")] private static partial Regex CellSplit();
    [GeneratedRegex(@"\([^)]*\)")] private static partial Regex Parenthesised();
    [GeneratedRegex(@"(?:ಸಂಖ್ಯೆ|#)\s*:?\s*(\d{6})(?!\d)")] private static partial Regex GrievanceNumber();
    [GeneratedRegex(@"[ಀ-೿]")] private static partial Regex Kannada();
    // A real English sentence has at least one of these; romanised Kannada ("tamma pinchani ... maru chalti") has none.
    [GeneratedRegex(@"\b(?:the|a|an|is|are|was|were|has|have|had|been|be|will|not|no|to|of|and|for|your|their|his|her)\b", RegexOptions.IgnoreCase)]
    private static partial Regex EnglishWord();
    // "A, B", "A / B", "A ಮತ್ತು B" (and): several schemes instead of one.
    [GeneratedRegex(@"[,/;]| ಮತ್ತು ")] private static partial Regex SchemeList();
    [GeneratedRegex(@"^:?-*:?\z")] private static partial Regex SeparatorCell();
    [GeneratedRegex(@"^\d{6}\z")] private static partial Regex SixDigits();
    [GeneratedRegex(@"\S{3,}")] private static partial Regex ThreeOrMoreChars();
}
