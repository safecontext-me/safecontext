"""Write scrub_report.md: what the scrubber does to the synthetic mailbox."""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from safecontext.scrub import Dropped, scrub  # noqa: E402
from safecontext.scrub.normalize import html_to_text  # noqa: E402
from safecontext.demo.synthetic import mailbox  # noqa: E402


def block(text):
    return "```\n" + (text.strip() or "(empty)") + "\n```"


def main():
    msgs = mailbox()
    results = [(m, scrub(m)) for m in msgs]
    dropped = [(m, r) for m, r in results if isinstance(r, Dropped)]
    kept = [(m, r) for m, r in results if not isinstance(r, Dropped)]
    totals = Counter()
    for _, r in kept:
        totals.update(r.redactions)

    out = ["# Scrub report (synthetic mailbox)", "",
           f"{len(msgs)} messages: **{len(kept)} kept**, **{len(dropped)} dropped**. "
           f"{sum(totals.values())} inline redactions.", "",
           "## Dropped", "", "| From | Subject | Reason |", "|---|---|---|"]
    for m, r in dropped:
        out.append(f"| {m.header('from')} | {m.header('subject')} | `{r.reason}` |")
    out += ["", "## Redactions by type", "", "| Type | Count |", "|---|---|"]
    for k, v in totals.most_common():
        out.append(f"| {k} | {v} |")
    out += ["", "## Kept messages: before and after", ""]
    for m, r in kept:
        before = m.text_body or html_to_text(m.html_body)
        out += [f"### {m.header('subject')}  ", f"From {m.header('from')}", "",
                "**Before**", block(before), "**After**", block(r.body)]
        if m.attachments:
            out.append("Attachments: " + ", ".join(
                f"`{a.filename}` → `{b.filename}` ({b.mime_type}, {b.size:,} bytes)"
                for a, b in zip(m.attachments, r.attachments)))
        out.append("")
    path = ROOT / "scrub_report.md"
    path.write_text("\n".join(out))
    print(f"wrote {path.name}: {len(kept)} kept, {len(dropped)} dropped, {dict(totals)}")


if __name__ == "__main__":
    main()
