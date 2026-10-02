"""Score one set of verdicts against a reference set (e.g. Qwen against Gemini).

Both folders hold the <grievance_id>Verdict.json files written by app.compare_atr.

Usage:
    python -m app.compare_backends
    python -m app.compare_backends --reference Observation --candidate Verdict_Qwen
"""

import argparse
import json
import sys
from pathlib import Path

from app.compare_atr import VERDICT_INFO


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Each verdict has its own fixed formal remark, so the remark tells us the verdict.
VERDICT_BY_REMARK = {remark: verdict for verdict, (_, _, remark) in VERDICT_INFO.items()}


def load_verdicts(folder: Path) -> dict:
    return {
        path.name.removesuffix("Verdict.json"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*Verdict.json"))
    }


def main():
    parser = argparse.ArgumentParser(description="Compare two folders of verdicts.")
    parser.add_argument("--reference", type=Path, default=PROJECT_ROOT / "Observation")
    parser.add_argument("--candidate", type=Path, default=PROJECT_ROOT / "Verdict_Qwen")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    reference = load_verdicts(args.reference)
    candidate = load_verdicts(args.candidate)
    cases = sorted(reference.keys() & candidate.keys())
    if not cases:
        raise SystemExit("No grievance has a verdict in both folders.")

    print(f"Reference: {args.reference.name}   Candidate: {args.candidate.name}   Cases: {len(cases)}\n")
    print(f"{'Case':<8} {'Reference':<28} {'Candidate':<28}")
    verdict_agree = resolved_agree = 0
    for case in cases:
        ref, cand = reference[case], candidate[case]
        ref_verdict = VERDICT_BY_REMARK[ref["formal_remark"]]
        cand_verdict = VERDICT_BY_REMARK[cand["formal_remark"]]
        verdict_agree += ref_verdict == cand_verdict
        resolved_agree += ref["is_resolved"] == cand["is_resolved"]
        mark = "" if ref_verdict == cand_verdict else "  <-- differs"
        print(
            f"{case:<8} {ref_verdict + (' (resolved)' if ref['is_resolved'] else ''):<28} "
            f"{cand_verdict + (' (resolved)' if cand['is_resolved'] else ''):<28}{mark}"
        )

    print(f"\nis_resolved agreement: {resolved_agree}/{len(cases)} ({resolved_agree / len(cases):.0%})")
    print(f"verdict agreement:     {verdict_agree}/{len(cases)} ({verdict_agree / len(cases):.0%})")

    only = sorted(reference.keys() ^ candidate.keys())
    if only:
        print(f"\nIn only one folder (not compared): {', '.join(only)}")


if __name__ == "__main__":
    main()
