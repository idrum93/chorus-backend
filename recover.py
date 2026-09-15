#!/usr/bin/env python3
"""Salvage the articles table from a damaged chorus.db.

Every other table — grams, cooc, gramcc, totals, mentions — is computed from
articles, so articles is the only thing that cannot be replaced. This reads
what it can, row by row, skipping damaged pages, and writes a fresh file.

    python recover.py chorus.db recovered.db

Then run: python collect.py --rebuild
"""
import sqlite3
import sys


def salvage(src_path, dst_path):
    src = sqlite3.connect(src_path)

    cols = None
    try:
        cols = [r[1] for r in src.execute("PRAGMA table_info(articles)")]
    except sqlite3.DatabaseError as e:
        print(f"  cannot read the articles schema: {e}")
        return 1
    if not cols:
        print("  no articles table found — the damage is worse than expected")
        return 1
    print(f"  articles has {len(cols)} columns: {', '.join(cols)}")

    # damaged rows may not be valid UTF-8, so take them as bytes and let
    # sqlite store them back as they are
    src.text_factory = bytes
    dst = sqlite3.connect(dst_path)
    dst.text_factory = bytes
    dst.execute(f"CREATE TABLE articles({', '.join(cols)})")

    # read by rowid so one bad page does not stop the rest
    lo = hi = None
    try:
        lo, hi = src.execute("SELECT MIN(rowid), MAX(rowid) FROM articles").fetchone()
    except sqlite3.DatabaseError:
        pass
    if hi is None:
        # the table is too damaged to summarise; find the top by doubling
        # until nothing is there, so the pass below stays proportional
        probe = 1024
        while probe < 10 ** 8:
            try:
                if not src.execute("SELECT 1 FROM articles WHERE rowid>=? "
                                   "LIMIT 1", (probe,)).fetchone():
                    break
            except sqlite3.DatabaseError:
                pass
            probe *= 2
        lo, hi = 1, probe
    if lo is None:
        print("  articles is empty")
        return 1
    print(f"  rows {lo:,} to {hi:,}")

    placeholders = ",".join("?" * len(cols))
    sel = f"SELECT {', '.join(cols)} FROM articles WHERE rowid=?"
    kept = lost = 0

    # Blocks first, because it is much faster where the file is sound.
    # A damaged block returns fewer rows than it should without raising, so
    # anything it misses is picked up by the row-by-row pass below.
    seen = set()
    step = 500
    at = lo
    while at <= hi:
        end = min(at + step - 1, hi)
        try:
            rows = list(src.execute(
                f"SELECT rowid, {', '.join(cols)} FROM articles "
                "WHERE rowid BETWEEN ? AND ?", (at, end)))
            for r in rows:
                dst.execute(f"INSERT INTO articles VALUES({placeholders})", r[1:])
                seen.add(r[0])
                kept += 1
        except sqlite3.DatabaseError:
            pass
        at = end + 1

    print(f"  {kept:,} read in blocks, checking the gaps one at a time")
    for r in range(lo, hi + 1):
        if r in seen:
            continue
        try:
            row = src.execute(sel, (r,)).fetchone()
            if row:
                dst.execute(f"INSERT INTO articles VALUES({placeholders})", row)
                kept += 1
        except sqlite3.DatabaseError:
            lost += 1

    dst.commit()
    print(f"\n  recovered {kept:,} articles, lost {lost:,}")
    n = dst.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"  the new file holds {n:,}")
    dst.close()
    return 0 if kept else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(salvage(sys.argv[1], sys.argv[2]))
