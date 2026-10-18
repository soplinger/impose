import pathlib
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from fractions import Fraction

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import impose  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"


def write_job(tmp_path, body):
    path = tmp_path / "job.job"
    path.write_text(body, encoding="utf-8")
    return str(path)


BASE = """\
title = Test
trim = 50 x 70 mm
sheet = A4
pages = 8
bleed = 0
margin = 10
gap = 5
"""


def test_parse_length_units():
    assert impose.parse_length("3") == 3
    assert impose.parse_length("1 cm") == 10
    assert impose.parse_length("1in") == Fraction(254, 10)
    with pytest.raises(ValueError):
        impose.parse_length("wide")


def test_parse_size_named_and_custom():
    assert impose.parse_size("A5") == (148, 210)
    assert impose.parse_size("148 x 210 mm") == (148, 210)
    assert impose.parse_size("8.5 x 11 in") == (Fraction(85, 10) * Fraction(254, 10), 11 * Fraction(254, 10))
    with pytest.raises(ValueError):
        impose.parse_size("big")


def test_parse_money_is_exact_and_rejects_negative():
    assert impose.parse_money("0.14") == Decimal("0.14")
    with pytest.raises(ValueError):
        impose.parse_money("-1")
    with pytest.raises(ValueError):
        impose.parse_money("free")


def test_saddle_order_for_eight_pages():
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=8)
    assert impose.imposition(job) == [((8, 1), (2, 7)), ((6, 3), (4, 5))]


def test_padding_adds_blank_pages_to_multiple_of_four():
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=21)
    assert impose.padded_pages(job) == 24
    rows = impose.order_rows(impose.Job(trim=(50, 70), sheet=(210, 297), pages=21))
    blanks = [(n, side) for n, side, left, right in rows for page in (left, right) if page is None]
    assert len(blanks) == 3


def test_perfect_binding_uses_signatures():
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=24, binding="perfect", signature=8)
    sheets = impose.imposition(job)
    assert len(sheets) == 6
    assert sheets[0] == ((8, 1), (2, 7))
    assert sheets[2] == ((16, 9), (10, 15))


def test_gang_layout_prefers_rotation_when_it_fits_more():
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=8, bleed=0)
    layout = impose.gang_layout(job)
    assert layout.gangs == 4
    assert layout.rotated is True


def test_copies_share_sheets_only_within_one_sheet_number(tmp_path):
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=8, bleed=0, copies=10)
    assert impose.gang_layout(job).gangs == 4
    summary = impose.summarize(job)
    assert summary["sheets_per_copy"] == 2
    assert summary["sheets_run"] == 6  # 2 sheet numbers x ceil(10 / 4) physical sheets


def test_spread_meets_fold_with_bleed_only_on_outer_edges():
    job = impose.Job(trim=(50, 70), sheet=(210, 297), pages=8, bleed=3)
    layout = impose.gang_layout(job)
    assert layout.spread_w == 106 and layout.spread_h == 76
    root = ET.fromstring(impose.svg_document(job))
    rects = [r for r in root.iter(SVG_NS + "rect") if r.get("stroke") == "#000"]
    first_left, first_right = rects[0], rects[1]
    left_trim_right = float(first_left.get("x")) + float(first_left.get("width"))
    assert left_trim_right == pytest.approx(float(first_right.get("x")))  # trims meet at the fold


def test_trim_too_large_for_sheet_is_rejected(tmp_path):
    path = write_job(tmp_path, "trim = 300 x 300 mm\nsheet = A4\npages = 8\n")
    _, problems = impose.load_job(path)
    assert any(p.level == "error" and "does not fit" in p.message for p in problems)


def test_plan_sheets_spoilage_and_cost_are_exact(tmp_path):
    path = write_job(tmp_path, """\
trim = A5
sheet = 297 x 420 mm
pages = 8
bleed = 0
copies = 10
sheet_cost = 0.14
setup_cost = 30
bind_cost = 0.20
spoilage = 5
""")
    job, problems = impose.load_job(path)
    assert not [p for p in problems if p.level == "error"]
    summary = impose.summarize(job)
    assert summary["gangs"] == 1
    assert summary["sheets_run"] == 20
    assert summary["sheets_total"] == 21
    assert summary["cost"] == {"setup": "30.00", "paper": "2.94", "binding": "2.00",
                               "total": "34.94", "per_copy": "3.49"}


def test_spine_width_from_leaves_and_caliper(tmp_path):
    path = write_job(tmp_path, BASE.replace("pages = 8", "pages = 24") + "caliper = 0.1 mm\n")
    job, _ = impose.load_job(path)
    assert impose.summarize(job)["spine_mm"] == 1.2


def test_unknown_key_reports_its_line(tmp_path):
    path = write_job(tmp_path, "trim = A5\n\nsheet = A4\nwidth = 3\npages = 8\n")
    _, problems = impose.load_job(path)
    assert any(p.line == 4 and "unknown key 'width'" in p.message for p in problems)


def test_missing_required_key_is_an_error(tmp_path):
    _, problems = impose.load_job(write_job(tmp_path, "trim = A5\nsheet = A4\n"))
    assert any(p.level == "error" and "'pages'" in p.message for p in problems)


def test_perfect_binding_warns_about_many_blank_pages(tmp_path):
    path = write_job(tmp_path, BASE.replace("pages = 8", "pages = 17") + "binding = perfect\nsignature = 8\n")
    _, problems = impose.load_job(path)
    assert any(p.level == "warning" and "7 blank pages" in p.message for p in problems)


def test_bad_signature_is_rejected(tmp_path):
    path = write_job(tmp_path, BASE + "binding = perfect\nsignature = 6\n")
    _, problems = impose.load_job(path)
    assert any(p.level == "error" and p.line == 9 for p in problems)


def test_svg_is_well_formed_and_has_one_label_per_page_slot(tmp_path):
    job = impose.Job(title="A & B", trim=(50, 70), sheet=(210, 297), pages=8, bleed=0)
    gangs = impose.gang_layout(job).gangs
    root = ET.fromstring(impose.svg_document(job))
    page_labels = [t for t in root.iter(SVG_NS + "text") if t.get("text-anchor")]
    assert len(page_labels) == 2 * 2 * gangs * 2  # sheets x sides x gangs x pages per spread
    assert root.find(SVG_NS + "title").text == "A & B"


def test_order_csv_leaves_blank_pages_empty(tmp_path, capsys):
    path = write_job(tmp_path, BASE.replace("pages = 8", "pages = 6"))
    assert impose.main(["order", path, "--csv"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "sheet,side,left,right"
    assert lines[1] == "1,front,,1"
    assert lines[2] == "1,back,2,"


def test_cli_check_exit_codes(tmp_path, capsys):
    good = write_job(tmp_path, BASE)
    assert impose.main(["check", good]) == 0
    bad = write_job(tmp_path, BASE + "copies = 0\n")
    assert impose.main(["check", bad]) == 1
    assert "copies must be at least 1" in capsys.readouterr().err


def test_example_job_plans_cleanly(capsys):
    example = str(pathlib.Path(__file__).resolve().parents[1] / "examples" / "zine.job")
    assert impose.main(["plan", example, "--json"]) == 0
    assert '"sheets_total"' in capsys.readouterr().out
