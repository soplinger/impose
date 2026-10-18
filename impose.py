#!/usr/bin/env python3
"""Impose: offline booklet imposition and print-cost planner.

Reads a plain-text job file describing a booklet and works out the page
order for every printed sheet side, how many copies fit on one sheet, the
sheet count, exact costs, the spine width, and a to-scale SVG layout.
Standard library only; no network access.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from html import escape
from typing import Dict, List, NamedTuple, Optional, Tuple

CENT = Decimal("0.01")
Size = Tuple[Fraction, Fraction]

UNIT_MM = {"mm": Fraction(1), "cm": Fraction(10), "in": Fraction(254, 10)}

NAMED_SIZES: Dict[str, Size] = {
    "a0": (Fraction(841), Fraction(1189)),
    "a1": (Fraction(594), Fraction(841)),
    "a2": (Fraction(420), Fraction(594)),
    "a3": (Fraction(297), Fraction(420)),
    "a4": (Fraction(210), Fraction(297)),
    "a5": (Fraction(148), Fraction(210)),
    "a6": (Fraction(105), Fraction(148)),
    "b5": (Fraction(176), Fraction(250)),
    "sra3": (Fraction(320), Fraction(450)),
    "letter": (Fraction("215.9"), Fraction("279.4")),
    "legal": (Fraction("215.9"), Fraction("355.6")),
    "tabloid": (Fraction("279.4"), Fraction("431.8")),
}

_LENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|in)?")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(mm|cm|in)?")


# ---------------------------------------------------------------- parsing

def parse_length(text: str) -> Fraction:
    match = _LENGTH.fullmatch(text.strip().lower())
    if not match:
        raise ValueError(f"expected a length such as '3' or '3 mm', got {text!r}")
    return Fraction(match.group(1)) * UNIT_MM[match.group(2) or "mm"]


def parse_size(text: str) -> Size:
    key = text.strip().lower()
    if key in NAMED_SIZES:
        return NAMED_SIZES[key]
    match = _SIZE.fullmatch(key)
    if not match:
        raise ValueError(f"expected a sheet name or 'W x H unit', got {text!r}")
    unit = UNIT_MM[match.group(3) or "mm"]
    return Fraction(match.group(1)) * unit, Fraction(match.group(2)) * unit


def parse_int(text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"expected a whole number, got {text!r}")
    return int(text)


def parse_money(text: str) -> Decimal:
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"expected an amount such as 0.14, got {text!r}") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("amounts must be finite and not negative")
    return amount


def parse_percent(text: str) -> Fraction:
    body = text.strip().rstrip("%").strip()
    try:
        value = Fraction(body)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"expected a percentage such as 5 %, got {text!r}") from None
    if not 0 <= value <= 100:
        raise ValueError("spoilage must be between 0 and 100 percent")
    return value


def parse_binding(text: str) -> str:
    value = text.strip().lower()
    if value not in ("saddle", "perfect"):
        raise ValueError("binding must be 'saddle' or 'perfect'")
    return value


@dataclass
class Job:
    title: str = "Untitled"
    trim: Optional[Size] = None
    sheet: Optional[Size] = None
    pages: Optional[int] = None
    binding: str = "saddle"
    signature: int = 8
    copies: int = 1
    bleed: Fraction = Fraction(3)
    margin: Fraction = Fraction(10)
    gap: Fraction = Fraction(5)
    sheet_cost: Decimal = Decimal("0")
    setup_cost: Decimal = Decimal("0")
    bind_cost: Decimal = Decimal("0")
    spoilage: Fraction = Fraction(0)
    caliper: Fraction = Fraction("0.1")


FIELDS = {
    "title": ("title", str),
    "trim": ("trim", parse_size),
    "sheet": ("sheet", parse_size),
    "pages": ("pages", parse_int),
    "binding": ("binding", parse_binding),
    "signature": ("signature", parse_int),
    "copies": ("copies", parse_int),
    "bleed": ("bleed", parse_length),
    "margin": ("margin", parse_length),
    "gap": ("gap", parse_length),
    "sheet_cost": ("sheet_cost", parse_money),
    "setup_cost": ("setup_cost", parse_money),
    "bind_cost": ("bind_cost", parse_money),
    "spoilage": ("spoilage", parse_percent),
    "caliper": ("caliper", parse_length),
}
REQUIRED = ("trim", "sheet", "pages")


class Problem(NamedTuple):
    line: int
    level: str
    message: str

    def render(self, path: str) -> str:
        where = f"{path}:{self.line}" if self.line else path
        return f"{where}: {self.level}: {self.message}"


def read_job(text: str) -> Tuple[Job, List[Problem], Dict[str, int]]:
    """Parse 'key = value' lines. Returns the job, problems, and key -> line map."""
    job = Job()
    problems: List[Problem] = []
    lines: Dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        content = raw.split("#", 1)[0].strip()
        if not content:
            continue
        if "=" not in content:
            problems.append(Problem(number, "error", "expected 'key = value'"))
            continue
        key, value = (part.strip() for part in content.split("=", 1))
        key = key.lower()
        if key not in FIELDS:
            problems.append(Problem(number, "error", f"unknown key '{key}'"))
            continue
        if key in lines:
            problems.append(Problem(number, "error", f"duplicate key '{key}' (first on line {lines[key]})"))
            continue
        lines[key] = number
        attr, parser = FIELDS[key]
        try:
            setattr(job, attr, parser(value))
        except ValueError as exc:
            problems.append(Problem(number, "error", f"{key}: {exc}"))
    for key in REQUIRED:
        if key not in lines:
            problems.append(Problem(0, "error", f"missing required key '{key}'"))
    return job, problems, lines


# ------------------------------------------------------------- geometry

@dataclass(frozen=True)
class Layout:
    sheet_w: Fraction
    sheet_h: Fraction
    across: int
    down: int
    rotated: bool
    trim_w: Fraction
    trim_h: Fraction
    bleed: Fraction
    gap: Fraction
    margin: Fraction

    @property
    def gangs(self) -> int:
        return self.across * self.down

    @property
    def spread_w(self) -> Fraction:
        return 2 * self.trim_w + 2 * self.bleed

    @property
    def spread_h(self) -> Fraction:
        return self.trim_h + 2 * self.bleed


def gang_layout(job: Job) -> Optional[Layout]:
    """Most copies of one folded spread that fit on the sheet, trying both orientations.

    A spread is two trimmed pages meeting at the fold. Bleed sits on the outer
    edges only, because the fold is never trimmed. Returns None when a single
    spread does not fit inside the sheet margins.
    """
    trim_w, trim_h = job.trim
    spread_w = 2 * trim_w + 2 * job.bleed
    spread_h = trim_h + 2 * job.bleed
    best: Optional[Layout] = None
    for rotated in (False, True):
        sheet_w, sheet_h = (job.sheet[1], job.sheet[0]) if rotated else job.sheet
        across = max(0, math.floor((sheet_w - 2 * job.margin + job.gap) / (spread_w + job.gap)))
        down = max(0, math.floor((sheet_h - 2 * job.margin + job.gap) / (spread_h + job.gap)))
        if across * down and (best is None or across * down > best.gangs):
            best = Layout(sheet_w, sheet_h, across, down, rotated,
                          trim_w, trim_h, job.bleed, job.gap, job.margin)
    return best


# ----------------------------------------------------------- imposition

def padded_pages(job: Job) -> int:
    """Page count rounded up to a whole booklet (multiple of 4) or whole signature."""
    unit = job.signature if job.binding == "perfect" else 4
    return -(-job.pages // unit) * unit


def booklet_sheets(count: int) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Saddle-stitch order for a page count that is a multiple of 4.

    Each sheet is ((front_left, front_right), (back_left, back_right)); the fold
    sits between left and right on both sides.
    """
    sheets = []
    for step in range(count // 4):
        inner = 2 * step
        sheets.append(((count - inner, 1 + inner), (2 + inner, count - 1 - inner)))
    return sheets


def imposition(job: Job) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    total = padded_pages(job)
    if job.binding == "saddle":
        return booklet_sheets(total)
    sheets = []
    for start in range(0, total, job.signature):
        for (f_left, f_right), (b_left, b_right) in booklet_sheets(job.signature):
            sheets.append(((f_left + start, f_right + start), (b_left + start, b_right + start)))
    return sheets


def order_rows(job: Job) -> List[Tuple[int, str, Optional[int], Optional[int]]]:
    """One row per sheet side. Blank (padding) pages are None."""
    rows = []
    for number, (front, back) in enumerate(imposition(job), 1):
        for side, spread in (("front", front), ("back", back)):
            left, right = (None if page > job.pages else page for page in spread)
            rows.append((number, side, left, right))
    return rows


def _show(page: Optional[int]) -> str:
    return "blank" if page is None else str(page)


def order_text(job: Job) -> str:
    return "".join(
        f"sheet {number:>3}  {side:<5}  {_show(left)} | {_show(right)}\n"
        for number, side, left, right in order_rows(job)
    )


def order_csv(job: Job) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["sheet", "side", "left", "right"])
    for number, side, left, right in order_rows(job):
        writer.writerow([number, side, "" if left is None else left, "" if right is None else right])
    return buffer.getvalue()


def diagram_text(job: Job) -> str:
    lines = []
    for number, (front, back) in enumerate(imposition(job), 1):
        lines.append(f"Sheet {number}")
        for side, spread in (("front", front), ("back", back)):
            cells = ["blank" if page > job.pages else str(page) for page in spread]
            border = "+" + "+".join("-" * 9 for _ in cells) + "+"
            row = "|" + "|".join(f"{cell:^9}" for cell in cells) + "|"
            lines.append(f"  {side}")
            lines.append("  " + border)
            lines.append("  " + row)
            lines.append("  " + border)
            lines.append("  " + f"{'fold':^{len(border)}}")
        lines.append("")
    return "\n".join(lines)


def _f(value: Fraction) -> str:
    return f"{float(value):.2f}"


def _page_svg(job: Job, layout: Layout, x: Fraction, y: Fraction, number: int, trim_offset: Fraction) -> List[str]:
    """One page: its cut box (trim plus outer bleed) and its trim box inside it."""
    blank = number > job.pages
    fill = "#e9e9e9" if blank else "#ffffff"
    cut_w = layout.trim_w + layout.bleed
    size = min(layout.trim_w, layout.trim_h) / 6
    label = "blank" if blank else str(number)
    return [
        f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(cut_w)}" height="{_f(layout.spread_h)}" '
        f'fill="{fill}" stroke="#c33" stroke-width="0.15" stroke-dasharray="1 1"/>',
        f'<rect x="{_f(x + trim_offset)}" y="{_f(y + layout.bleed)}" '
        f'width="{_f(layout.trim_w)}" height="{_f(layout.trim_h)}" fill="none" stroke="#000" stroke-width="0.2"/>',
        f'<text x="{_f(x + trim_offset + layout.trim_w / 2)}" y="{_f(y + layout.spread_h / 2)}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="sans-serif" font-size="{_f(size)}" fill="#666">{label}</text>',
    ]


def _side_svg(job: Job, layout: Layout, left: int, right: int, top: Fraction, label: str) -> List[str]:
    width, height = layout.sheet_w, layout.sheet_h
    block_w = layout.spread_w
    used_w = layout.across * block_w + (layout.across - 1) * layout.gap
    used_h = layout.down * layout.spread_h + (layout.down - 1) * layout.gap
    x0 = (width - used_w) / 2
    y0 = top + (height - used_h) / 2
    parts = [
        f'<rect x="0" y="{_f(top)}" width="{_f(width)}" height="{_f(height)}" fill="none" stroke="#888" stroke-width="0.2"/>',
        f'<text x="2" y="{_f(top + 4)}" font-family="sans-serif" font-size="3" fill="#555">{label}</text>',
    ]
    for row in range(layout.down):
        for col in range(layout.across):
            x = x0 + col * (block_w + layout.gap)
            y = y0 + row * (layout.spread_h + layout.gap)
            fold = x + layout.trim_w + layout.bleed
            parts.append(
                f'<line x1="{_f(fold)}" y1="{_f(y)}" x2="{_f(fold)}" y2="{_f(y + layout.spread_h)}" '
                f'stroke="#36c" stroke-width="0.2" stroke-dasharray="2 2"/>'
            )
            parts.extend(_page_svg(job, layout, x, y, left, layout.bleed))
            parts.extend(_page_svg(job, layout, fold, y, right, 0))
    return parts


def svg_document(job: Job) -> str:
    """To-scale SVG in millimetres: one panel per sheet side, stacked vertically."""
    layout = gang_layout(job)
    spacing = Fraction(10)
    sides = []
    for number, (front, back) in enumerate(imposition(job), 1):
        sides.append((f"Sheet {number} front", front))
        sides.append((f"Sheet {number} back", back))
    height = len(sides) * (layout.sheet_h + spacing) - spacing
    body = []
    top = Fraction(0)
    for label, (left, right) in sides:
        body.extend(_side_svg(job, layout, left, right, top, label))
        top += layout.sheet_h + spacing
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(layout.sheet_w)}mm" '
        f'height="{_f(height)}mm" viewBox="0 0 {_f(layout.sheet_w)} {_f(height)}">',
        f"  <title>{escape(job.title)}</title>",
    ]
    return "\n".join(head + ["  " + line for line in body] + ["</svg>"]) + "\n"


# ------------------------------------------------------- validation & plan

def validate(job: Job, lines: Dict[str, int]) -> List[Problem]:
    problems: List[Problem] = []

    def add(key: str, level: str, message: str) -> None:
        problems.append(Problem(lines.get(key, 0), level, message))

    if job.pages is not None and job.pages < 1:
        add("pages", "error", "pages must be at least 1")
    if job.copies < 1:
        add("copies", "error", "copies must be at least 1")
    bad_signature = job.binding == "perfect" and (job.signature % 4 or not 4 <= job.signature <= 64)
    if bad_signature:
        add("signature", "error", "signature must be a multiple of 4 from 4 to 64")
    elif job.binding == "saddle" and "signature" in lines:
        add("signature", "warning", "signature is ignored for saddle binding")
    for key, size in (("trim", job.trim), ("sheet", job.sheet)):
        if size is not None and min(size) <= 0:
            add(key, "error", "both dimensions must be greater than zero")

    if bad_signature or job.trim is None or job.sheet is None or job.pages is None:
        return problems
    if job.pages < 1 or min(job.trim) <= 0 or min(job.sheet) <= 0:
        return problems
    layout = gang_layout(job)
    if layout is None:
        add("trim", "error",
            f"spread with bleed ({_dims(2 * job.trim[0] + 2 * job.bleed, job.trim[1] + 2 * job.bleed)}) "
            f"does not fit on sheet {_dims(*job.sheet)} with a {_num(job.margin)} mm margin")
        return problems
    blanks = padded_pages(job) - job.pages
    if blanks >= 4:
        add("pages", "warning", f"{blanks} blank pages will be added to fill the last signature")
    return problems


def load_job(path: str) -> Tuple[Job, List[Problem]]:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    job, problems, lines = read_job(text)
    problems.extend(validate(job, lines))
    return job, problems


def _num(value) -> float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 3)


def _dims(width, height) -> str:
    return f"{_num(width)} x {_num(height)} mm"


def _money(amount: Decimal) -> str:
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def summarize(job: Job) -> dict:
    layout = gang_layout(job)
    total = padded_pages(job)
    sheets_per_copy = total // 4
    # Each physical sheet carries one sheet number, `gangs` times over, so each
    # sheet number needs its own ceiling.
    sheets_run = sheets_per_copy * -(-job.copies // layout.gangs)
    sheets_total = math.ceil(Fraction(sheets_run) * (100 + job.spoilage) / 100)
    paper = sheets_total * job.sheet_cost
    binding = job.copies * job.bind_cost
    total_cost = job.setup_cost + paper + binding
    leaves = total // 2
    return {
        "title": job.title,
        "binding": job.binding,
        "trim": _dims(*job.trim),
        "bleed_mm": _num(job.bleed),
        "spread": _dims(layout.spread_w, layout.spread_h),
        "sheet": _dims(layout.sheet_w, layout.sheet_h),
        "rotated": layout.rotated,
        "across": layout.across,
        "down": layout.down,
        "gangs": layout.gangs,
        "pages_requested": job.pages,
        "pages_imposed": total,
        "blank_pages": total - job.pages,
        "sheets_per_copy": sheets_per_copy,
        "copies": job.copies,
        "sheets_run": sheets_run,
        "spoilage_percent": _num(job.spoilage),
        "sheets_total": sheets_total,
        "cost": {
            "setup": _money(job.setup_cost),
            "paper": _money(paper),
            "binding": _money(binding),
            "total": _money(total_cost),
            "per_copy": _money(total_cost / job.copies),
        },
        "spine_mm": _num(leaves * job.caliper),
    }


def plan_text(s: dict) -> str:
    c = s["cost"]
    return "\n".join([
        f"{s['title']} ({s['binding']} binding)",
        f"  trim      {s['trim']}, bleed {s['bleed_mm']} mm, spread {s['spread']}",
        f"  sheet     {s['sheet']}{' (rotated)' if s['rotated'] else ''}",
        f"  layout    {s['across']} across x {s['down']} down = {s['gangs']} copy-spread(s) per sheet side",
        f"  pages     {s['pages_requested']} requested, {s['pages_imposed']} imposed, {s['blank_pages']} blank",
        f"  sheets    {s['sheets_per_copy']} per copy, {s['sheets_run']} for {s['copies']} copies, "
        f"{s['sheets_total']} with {s['spoilage_percent']} % spoilage",
        f"  cost      setup {c['setup']} + paper {c['paper']} + binding {c['binding']} = {c['total']}",
        f"  per copy  {c['per_copy']}",
        f"  spine     {s['spine_mm']} mm",
    ]) + "\n"


# --------------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="impose", description="Offline booklet imposition and print-cost planner.")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="lint a job file")
    check.add_argument("job")
    check.add_argument("--strict", action="store_true", help="treat warnings as errors")

    plan_cmd = commands.add_parser("plan", help="sheet count, costs and spine width")
    plan_cmd.add_argument("job")
    plan_cmd.add_argument("--json", action="store_true", help="print JSON instead of text")

    order = commands.add_parser("order", help="page numbers on every sheet side")
    order.add_argument("job")
    order.add_argument("--csv", action="store_true", help="print CSV; blank pages are empty cells")

    diagram = commands.add_parser("diagram", help="ASCII boxes for every sheet side")
    diagram.add_argument("job")

    svg = commands.add_parser("svg", help="to-scale SVG layout of every sheet side")
    svg.add_argument("job")
    svg.add_argument("-o", "--output", help="write to FILE instead of stdout")

    args = parser.parse_args(argv)
    try:
        job, problems = load_job(args.job)
    except OSError as exc:
        print(f"impose: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem.render(args.job), file=sys.stderr)
    errors = [p for p in problems if p.level == "error"]
    warnings = len(problems) - len(errors)
    if errors or (args.command == "check" and args.strict and warnings):
        return 1

    if args.command == "check":
        print(f"{args.job}: ok ({warnings} warning{'' if warnings == 1 else 's'})")
    elif args.command == "plan":
        summary = summarize(job)
        print(json.dumps(summary, indent=2) if args.json else plan_text(summary), end="")
    elif args.command == "order":
        print(order_csv(job) if args.csv else order_text(job), end="")
    elif args.command == "diagram":
        print(diagram_text(job))
    elif args.command == "svg":
        document = svg_document(job)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(document)
        else:
            print(document, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
