# Impose

**Offline booklet imposition and print-cost planner.** Describe a booklet in one plain-text file and get the page order for every printed sheet, how many copies fit on each sheet, the exact sheet count and cost, the spine width, and a to-scale SVG you can print.

Pure Python 3 standard library. No servers, no accounts, no network calls.

## Features

- **Plain-text job files** with `key = value` lines, `#` comments, and length units in `mm`, `cm` or `in`.
- **Line-numbered diagnostics**: every error and warning points at `file:line`, and `check` exits non-zero on errors (or on warnings with `--strict`).
- **Named sheet sizes** (A0–A6, B5, SRA3, Letter, Legal, Tabloid) or any custom `W x H unit`.
- **Saddle-stitch imposition**: the classic outer-to-inner page order for any booklet, with padding to a multiple of 4.
- **Perfect-binding signatures**: pages split into signatures of 8, 12, 16 … pages, each imposed as its own booklet.
- **Blank-page padding report** with a warning when a signature would leave four or more blank pages.
- **Gang layout**: works out how many copies of a spread fit on one sheet, trying both orientations and keeping the better one, with bleed, sheet margins and gutters.
- **Exact sheet count and costs** using decimal money and fractions: setup, paper, binding and cost per copy, with percentage spoilage.
- **Spine width** from page count and paper caliper.
- **ASCII diagrams** of every sheet side, with the fold marked.
- **SVG export** to scale in millimetres, with trim lines, bleed lines, fold lines and page labels.
- **CSV page order** for handing to a prepress tool, with blank pages as empty cells.
- **JSON plan output** for scripts and CI checks.

## Quick start

The repository includes `examples/zine.job`, a 22-page zine printed on A3 and folded to A5:

```
title = Field Notes
trim = A5
sheet = A3
pages = 22
binding = saddle
copies = 100
bleed = 3 mm
margin = 10 mm
gap = 5 mm
sheet_cost = 0.14
setup_cost = 30
bind_cost = 0.20
spoilage = 5 %
caliper = 0.10 mm
```

```bash
python3 impose.py check examples/zine.job
python3 impose.py plan examples/zine.job
```

`plan` prints:

```
Field Notes (saddle binding)
  trim      148 x 210 mm, bleed 3 mm, spread 302 x 216 mm
  sheet     420 x 297 mm (rotated)
  layout    1 across x 1 down = 1 copy-spread(s) per sheet side
  pages     22 requested, 24 imposed, 2 blank
  sheets    6 per copy, 600 for 100 copies, 630 with 5 % spoilage
  cost      setup 30.00 + paper 88.20 + binding 20.00 = 138.20
  per copy  1.38
  spine     1.2 mm
```

`order` shows where each page goes. The first two sheets look like this:

```
sheet   1  front  blank | 1
sheet   1  back   2 | blank
sheet   2  front  22 | 3
sheet   2  back   4 | 21
```

Save a to-scale layout for printing:

```bash
python3 impose.py svg examples/zine.job -o zine-layout.svg
```

## Job file reference

| Key | Default | Meaning |
| --- | --- | --- |
| `title` | `Untitled` | Name shown in plans and the SVG title |
| `trim` | required | Final page size, e.g. `A5` or `148 x 210 mm` |
| `sheet` | required | Printed sheet size before folding, e.g. `A3` or `297 x 420 mm` |
| `pages` | required | Number of pages in the booklet |
| `binding` | `saddle` | `saddle` (stapled) or `perfect` (signatures) |
| `signature` | `8` | Pages per signature for `perfect`; a multiple of 4 from 4 to 64 |
| `copies` | `1` | Number of finished copies |
| `bleed` | `3 mm` | Extra image area beyond the trim on each outer edge (the fold is never trimmed, so pages meet at the fold without bleed) |
| `margin` | `10 mm` | Unprinted sheet margin (grippers) |
| `gap` | `5 mm` | Space between neighbouring spreads |
| `sheet_cost` | `0` | Cost of one printed sheet (paper and ink) |
| `setup_cost` | `0` | Fixed cost for the job |
| `bind_cost` | `0` | Cost of binding one copy |
| `spoilage` | `0 %` | Extra sheets printed to cover misprints |
| `caliper` | `0.1 mm` | Paper thickness per leaf, used for spine width |

Money is exact decimal arithmetic and is rounded half-up to cents only when printed.

## Commands

```
python3 impose.py check    JOB [--strict]
python3 impose.py plan     JOB [--json]
python3 impose.py order    JOB [--csv]
python3 impose.py diagram  JOB
python3 impose.py svg      JOB [-o FILE]
```

Exit codes: `0` success, `1` the job has errors (or warnings under `check --strict`), `2` the file could not be read.

## Assumptions and limits

- Each sheet is folded once, with the fold between the two pages of every spread. Folds run across the sheet, so a single fold is the only fold modelled.
- A physical sheet carries `gangs` copies of one sheet number, so each sheet number needs `ceil(copies / gangs)` sheets. Sheets for different sheet numbers are never mixed on one sheet.
- The planner does not produce print files, PDFs, or imposition plates. Use the SVG or CSV to check layouts and hand them to your printer.
- Costs are whatever you put in the job file. Impose does no pricing lookups.

## Running the tests

```bash
pip install pytest
python3 -m pytest tests
```

The tests cover unit parsing, saddle and signature ordering, blank-page padding, gang layout and rotation, sheet fit, exact cost and spoilage arithmetic, spine width, line-numbered lint errors, the SVG output (parsed as XML), CSV output, and CLI exit codes.
