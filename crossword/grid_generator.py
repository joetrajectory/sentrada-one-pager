#!/usr/bin/env python3
"""
Sentrada crossword grid generator.

Takes 25-30 candidate answer/clue pairs and places the best-fitting 15-20 into a
British-style (open) crossword grid: words cross one another, only the cells that
carry a letter exist, and the paper shows through every gap. This is NOT an
American-style solid rectangle with black blocking squares.

The placement rules enforce a valid open grid:

  - every word after the first must INTERSECT at least one already-placed word at
    a shared letter,
  - no RUN-ON words (the cell immediately before a word's start and after its end,
    along the word's axis, must be empty so two words never read as one longer
    word),
  - no ADJACENT PARALLEL words (a newly-laid letter may not sit directly beside an
    existing letter on its perpendicular axis unless that cell is the actual
    crossing point), so two words never run side by side.

Clue numbers are assigned left-to-right, top-to-bottom over the finished grid,
exactly as a printed crossword numbers its lights. The generator runs many
attempts from a seeded RNG and keeps the best result (most words placed, then the
most compact, most-crossed grid).

Used as a library by crossword.py (``generate_grid``) and runnable on its own for
debugging:

    python grid_generator.py --data test_cognism.json --seed 42 --ascii
"""

import argparse
import json
import random

ACROSS = "across"
DOWN = "down"

# Direction unit vectors: (d_row, d_col).
_DELTA = {ACROSS: (0, 1), DOWN: (1, 0)}


class Grid:
    """A sparse letter grid built on an (row, col) dict, materialised to a dense
    2D array only at the end. Coordinates may be negative during construction; the
    grid is normalised (shifted so the minimum row/col is zero) when finalised."""

    def __init__(self):
        self.cells = {}          # (r, c) -> letter
        self.placed = []         # list of placed-word dicts (no numbers yet)
        self.answers = set()     # answers already placed (dedupe)

    # -- queries -------------------------------------------------------------
    def letter(self, r, c):
        return self.cells.get((r, c))

    def occupied(self, r, c):
        return (r, c) in self.cells

    def bbox(self):
        if not self.cells:
            return 0, 0, 0, 0
        rs = [r for r, _ in self.cells]
        cs = [c for _, c in self.cells]
        return min(rs), min(cs), max(rs), max(cs)

    def dims(self):
        r0, c0, r1, c1 = self.bbox()
        return (r1 - r0 + 1, c1 - c0 + 1) if self.cells else (0, 0)

    # -- placement -----------------------------------------------------------
    def fits(self, answer, r, c, direction):
        """Return the number of intersections if `answer` can be legally placed at
        (r, c) going `direction`, or -1 if the placement is illegal. The first
        word (empty grid) is legal anywhere and returns 0 intersections."""
        dr, dc = _DELTA[direction]
        first = not self.cells
        crossings = 0

        # Run-on guard: the cells flanking the word along its own axis must be free.
        before = (r - dr, c - dc)
        after = (r + dr * len(answer), c + dc * len(answer))
        if self.occupied(*before) or self.occupied(*after):
            return -1

        for i, ch in enumerate(answer):
            rr, cc = r + dr * i, c + dc * i
            here = self.letter(rr, cc)
            if here is not None:
                if here != ch:
                    return -1            # conflicting letter
                crossings += 1           # legal crossing on a shared letter
                continue
            # Empty target cell: its perpendicular neighbours must be empty, else
            # this new letter would sit flush against a parallel word.
            if direction == ACROSS:
                if self.occupied(rr - 1, cc) or self.occupied(rr + 1, cc):
                    return -1
            else:
                if self.occupied(rr, cc - 1) or self.occupied(rr, cc + 1):
                    return -1

        if not first and crossings == 0:
            return -1                    # every later word must cross something
        return crossings

    def place(self, answer, clue, r, c, direction):
        dr, dc = _DELTA[direction]
        for i, ch in enumerate(answer):
            self.cells[(r + dr * i, c + dc * i)] = ch
        self.placed.append({
            "answer": answer, "clue": clue,
            "row": r, "col": c, "direction": direction,
        })
        self.answers.add(answer)

    # -- finishing -----------------------------------------------------------
    def finalize(self):
        """Normalise coordinates to a zero-based dense grid, assign clue numbers
        left-to-right/top-to-bottom, and split the words into across/down lists.
        Returns the dict the renderer consumes."""
        r0, c0, r1, c1 = self.bbox()
        rows, cols = r1 - r0 + 1, c1 - c0 + 1
        grid = [[None] * cols for _ in range(rows)]
        for (r, c), ch in self.cells.items():
            grid[r - r0][c - c0] = ch

        def starts_across(r, c):
            return (grid[r][c] is not None
                    and (c == 0 or grid[r][c - 1] is None)
                    and c + 1 < cols and grid[r][c + 1] is not None)

        def starts_down(r, c):
            return (grid[r][c] is not None
                    and (r == 0 or grid[r - 1][c] is None)
                    and r + 1 < rows and grid[r + 1][c] is not None)

        # Number every cell that begins an across or a down light.
        number_at = {}
        n = 0
        for r in range(rows):
            for c in range(cols):
                if starts_across(r, c) or starts_down(r, c):
                    n += 1
                    number_at[(r, c)] = n

        clue_of = {(w["row"] - r0, w["col"] - c0, w["direction"]): w["clue"]
                   for w in self.placed}
        ans_of = {(w["row"] - r0, w["col"] - c0, w["direction"]): w["answer"]
                  for w in self.placed}

        across, down, placed = [], [], []
        for (r, c), num in sorted(number_at.items(), key=lambda kv: kv[1]):
            for direction, bucket in ((ACROSS, across), (DOWN, down)):
                key = (r, c, direction)
                if key in ans_of:
                    entry = {"number": num, "answer": ans_of[key],
                             "clue": clue_of[key], "row": r, "col": c,
                             "direction": direction}
                    bucket.append(entry)
                    placed.append(entry)

        return {
            "rows": rows, "cols": cols, "grid": grid,
            "numbers": {f"{r},{c}": num for (r, c), num in number_at.items()},
            "placed": placed, "across": across, "down": down,
            "word_count": len(placed),
        }


def _candidate_word(cand):
    """Normalise a candidate answer to uppercase A-Z only (drop spaces/punctuation
    so multi-token answers still place as a single light)."""
    return "".join(ch for ch in cand["answer"].upper() if ch.isalpha())


def _normalize(s):
    """Normalise a raw answer string to uppercase A-Z (for anchor matching)."""
    return "".join(ch for ch in str(s).upper() if ch.isalpha())


def _attempt(cands, max_words, rng, anchors=frozenset()):
    """Build one grid greedily. ANCHORS are placed first (longest first), so the
    designated hero answers seed and shape the grid and have the best chance of a
    legal placement; the longest anchor opens the grid at the origin. The rest
    follow longest-first (the most crossing opportunities) with light jitter so
    repeated seeds explore variants. Returns a Grid (not yet finalised)."""
    anchor_cands = [c for c in cands if _candidate_word(c) in anchors]
    other = [c for c in cands if _candidate_word(c) not in anchors]
    anchor_cands.sort(key=lambda c: len(_candidate_word(c)), reverse=True)
    rng.shuffle(other)
    other.sort(key=lambda c: len(_candidate_word(c)), reverse=True)
    order = anchor_cands + other

    grid = Grid()
    for cand in order:
        if len(grid.placed) >= max_words:
            break
        word = _candidate_word(cand)
        if len(word) < 3 or word in grid.answers:
            continue
        if not grid.cells:
            grid.place(word, cand["clue"], 0, 0, ACROSS)
            continue

        best = None  # (score, r, c, direction)
        # Try crossing this word over every matching letter already on the grid.
        for (er, ec), ech in list(grid.cells.items()):
            for i, ch in enumerate(word):
                if ch != ech:
                    continue
                for direction in (ACROSS, DOWN):
                    dr, dc = _DELTA[direction]
                    r, c = er - dr * i, ec - dc * i
                    cross = grid.fits(word, r, c, direction)
                    if cross < 0:
                        continue
                    # Score: reward crossings heavily, then compactness (a small
                    # bounding box keeps the grid dense and central), with a touch
                    # of jitter to break ties differently across seeds.
                    r0, c0, r1, c1 = grid.bbox()
                    nr0, nc0 = min(r0, r), min(c0, c)
                    nr1 = max(r1, r + (dr * (len(word) - 1)))
                    nc1 = max(c1, c + (dc * (len(word) - 1)))
                    area = (nr1 - nr0 + 1) * (nc1 - nc0 + 1)
                    score = cross * 100 - area + rng.random()
                    if best is None or score > best[0]:
                        best = (score, r, c, direction)
        if best is not None:
            _, r, c, direction = best
            grid.place(word, cand["clue"], r, c, direction)
    return grid


def _quality(grid):
    """Rank a finished grid: more words first, then more compact (a squarer, denser
    block reads as a proper puzzle, not a sprawling cross)."""
    rows, cols = grid.dims()
    if rows == 0:
        return (0, 0)
    fill = len(grid.cells) / float(rows * cols)
    aspect = min(rows, cols) / float(max(rows, cols))
    return (len(grid.placed), round(fill * 0.6 + aspect * 0.4, 4))


def generate_grid(candidates, min_words=15, max_words=20, seed=None, attempts=400,
                  anchors=None):
    """Generate the best crossword grid from a candidate pool. Runs `attempts`
    seeded builds and keeps the strongest. Returns the finalised dict from
    Grid.finalize().

    ANCHORS are designated hero answers that should anchor the grid (not merely
    survive as clues). They come from candidates flagged ``"anchor": true`` and/or
    an explicit `anchors` list of raw answer strings. Each attempt places them
    first, and grid selection prioritises, in order: most anchors placed, then
    meeting `min_words`, then most words, then compactness. The result records
    `anchors` (all requested) and `anchors_missing` (any that could not be placed)
    so --check can fail when a designated anchor did not land."""
    anchor_set = {_candidate_word(c) for c in candidates if c.get("anchor")}
    anchor_set |= {_normalize(a) for a in (anchors or [])}
    anchor_set = frozenset(a for a in anchor_set if len(a) >= 3)

    base = random.Random(seed)
    best_grid, best_key = None, None

    for _ in range(attempts):
        rng = random.Random(base.random())
        grid = _attempt(candidates, max_words, rng, anchor_set)
        n_anchor = len(anchor_set & grid.answers)
        meets_min = 1 if len(grid.placed) >= min_words else 0
        # Composite ranking: anchors first, then meeting the floor, then the
        # usual (word count, compactness) from _quality.
        key = (n_anchor, meets_min) + _quality(grid)
        if best_key is None or key > best_key:
            best_key, best_grid = key, grid

    result = best_grid.finalize()
    placed_words = {e["answer"] for e in result["placed"]}
    result["anchors"] = sorted(anchor_set)
    result["anchors_missing"] = sorted(anchor_set - placed_words)
    result["short"] = result["word_count"] < min_words
    result["seed"] = seed
    return result


def to_ascii(result):
    """Render the grid as text for quick visual debugging."""
    rows, cols, grid = result["rows"], result["cols"], result["grid"]
    lines = []
    for r in range(rows):
        lines.append(" ".join((grid[r][c] or ".") for c in range(cols)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Generate a crossword grid from candidates.")
    ap.add_argument("--data", required=True, help="JSON file with a 'candidates' array")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--min-words", type=int, default=None)
    ap.add_argument("--max-words", type=int, default=None)
    ap.add_argument("--attempts", type=int, default=400)
    ap.add_argument("--ascii", action="store_true", help="print the grid as text")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as fh:
        data = json.load(fh)
    result = generate_grid(
        data["candidates"],
        min_words=args.min_words or data.get("min_words", 15),
        max_words=args.max_words or data.get("max_words", 20),
        seed=args.seed if args.seed is not None else data.get("seed"),
        attempts=args.attempts,
        anchors=data.get("anchors"),
    )
    print(f"placed {result['word_count']} words on a {result['rows']}x{result['cols']} grid "
          f"({len(result['across'])} across, {len(result['down'])} down)"
          + ("  [short of min_words]" if result["short"] else ""))
    if result["anchors"]:
        placed_anchors = [a for a in result["anchors"] if a not in result["anchors_missing"]]
        print(f"anchors: {len(placed_anchors)}/{len(result['anchors'])} placed"
              + (f"; MISSING: {', '.join(result['anchors_missing'])}" if result["anchors_missing"] else ""))
    if args.ascii:
        print()
        print(to_ascii(result))


if __name__ == "__main__":
    main()
