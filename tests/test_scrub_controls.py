"""Scrubber controls for text extracted from a file or PDF.

Built-in rules stay as they are. Extra patterns apply only on the file path.
Samples are fixtures, never a real person's data.
"""
import json

from safecontext.scrub.controls import (
    BUILTIN, PatternError, UserPattern, add_pattern, delete_pattern, load_patterns, preview,
    scrub_file_text, set_enabled,
)
from safecontext.scrub.redact import REDACTORS, redact
from safecontext.store import Store

SSN = "536-22-1987"  # the same synthetic number the demo mailbox uses


def test_catalog_lists_every_builtin_kind():
    listed = [kind for kind, _label, _detail in BUILTIN]
    assert listed == list(dict.fromkeys(kind for kind, _fn in REDACTORS))
    assert "SSN" in listed


def test_file_text_keeps_the_builtin_ssn_rule():
    text = f"Form copy. SSN {SSN}. Nothing else."
    cleaned, counts = scrub_file_text(text)
    assert SSN not in cleaned
    assert "[REDACTED:SSN]" in cleaned
    assert counts["SSN"] == 1


def test_extra_pattern_runs_only_on_extracted_file_text():
    note = "Badge EMP-4242 was on the form."
    pattern = UserPattern(id="p", name="Employee ID", kind="EMPLOYEE_ID", pattern=r"EMP-\d{4}")
    mail, _ = redact(note)
    assert "EMP-4242" in mail  # mail and calendar do not grow a new rule
    cleaned, counts = scrub_file_text(note, [pattern])
    assert "EMP-4242" not in cleaned
    assert "[REDACTED:EMPLOYEE_ID]" in cleaned
    assert counts["EMPLOYEE_ID"] == 1


def test_disabled_pattern_does_not_run():
    pattern = UserPattern(id="p", name="Employee ID", kind="EMPLOYEE_ID", pattern=r"EMP-\d{4}", enabled=False)
    cleaned, counts = scrub_file_text("Badge EMP-4242", [pattern])
    assert "EMP-4242" in cleaned and "EMPLOYEE_ID" not in counts


def test_a_broken_saved_pattern_is_skipped():
    pattern = UserPattern(id="p", name="Broken", kind="BROKEN", pattern=r"(")
    cleaned, _counts = scrub_file_text(f"still here {SSN}", [pattern])
    assert SSN not in cleaned and "still here" in cleaned


def test_add_disable_and_remove_change_extracted_text(tmp_path):
    store = Store(tmp_path / "mail.db")
    sample = "Badge EMP-4242 was printed on the form."
    created = add_pattern(store, "Employee ID", r"EMP-\d{4}")
    cleaned, counts = scrub_file_text(sample, load_patterns(store))
    assert "EMP-4242" not in cleaned and counts["EMPLOYEE_ID"] == 1

    set_enabled(store, created.id, False)
    cleaned, counts = scrub_file_text(sample, load_patterns(store))
    assert "EMP-4242" in cleaned and "EMPLOYEE_ID" not in counts

    delete_pattern(store, created.id)
    cleaned, counts = scrub_file_text(sample, load_patterns(store))
    assert load_patterns(store) == []
    assert "EMP-4242" in cleaned and "EMPLOYEE_ID" not in counts
    store.close()


def test_patterns_round_trip_in_the_store(tmp_path):
    store = Store(tmp_path / "mail.db")
    created = add_pattern(store, "Employee ID", r"EMP-\d{4}")
    again = Store(tmp_path / "mail.db")
    rows = load_patterns(again)
    assert [(row.id, row.kind, row.pattern, row.enabled) for row in rows] == [
        (created.id, "EMPLOYEE_ID", r"EMP-\d{4}", True)]
    set_enabled(again, created.id, False)
    assert load_patterns(again)[0].enabled is False
    delete_pattern(again, created.id)
    assert load_patterns(again) == []
    blob = json.dumps([dict(r) for r in again._db.execute("SELECT * FROM source_state")])
    assert "EMP-4242" not in blob
    store.close()
    again.close()


def test_bad_patterns_are_refused(tmp_path):
    store = Store(tmp_path / "mail.db")
    for name, pattern in (("", r"EMP-\d{4}"), ("Employee ID", ""), ("Employee ID", r"(a+)+"),
                          ("Employee ID", r"a*"), ("Employee ID", r"[unterminated")):
        try:
            add_pattern(store, name, pattern)
        except PatternError:
            pass
        else:
            raise AssertionError(f"accepted {name!r} {pattern!r}")
    assert load_patterns(store) == []
    store.close()


def test_preview_scrubs_a_sample_and_does_not_store_it(tmp_path):
    store = Store(tmp_path / "mail.db")
    add_pattern(store, "Employee ID", r"EMP-\d{4}")
    sample = f"SSN {SSN} and badge EMP-4242"
    cleaned, counts, truncated = preview(store, sample)
    assert truncated is False
    assert SSN not in cleaned and "EMP-4242" not in cleaned
    assert counts["SSN"] == 1 and counts["EMPLOYEE_ID"] == 1
    blob = json.dumps([dict(r) for r in store._db.execute("SELECT * FROM source_state")])
    assert sample not in blob and SSN not in blob and "EMP-4242" not in blob
    store.close()

