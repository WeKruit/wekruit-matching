"""Fast Postgres dedupe — server-side bulk SQL via temp table + JOIN.

Replaces per-row executemany loop with:
  1. COPY mapping into TEMP TABLE _id_map (single round-trip for all rows).
  2. DROP feedback FK.
  3. UPDATE feedback via JOIN.
  4. DELETE jobs via JOIN.
  5. UPDATE jobs (PK swap) via JOIN.
  6. ADD feedback FK back.
  7. COMMIT.

Single TX. ~10-30 seconds expected.
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import defaultdict

import psycopg

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import _sqlalchemy_url_to_libpq
from wekruit_matching.scraper.id_utils import generate_job_id


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn = _sqlalchemy_url_to_libpq(get_settings().database_url)
    print(f"[dedupe_fast] connecting …", flush=True)

    with psycopg.connect(dsn) as conn:
        # Disable statement_timeout at the session level BEFORE any query.
        # Supabase pooler sets a 30s timeout that kills our SELECT * FROM jobs (212k rows).
        conn.autocommit = True
        with conn.cursor() as c0:
            c0.execute("SET statement_timeout = 0")
            c0.execute("SET lock_timeout = 0")
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, source_repo, company_name, role_title, first_seen_at, "
                "       last_seen_at, status, sources "
                "FROM jobs"
            )
            rows = cur.fetchall()
            print(f"[dedupe_fast] scanned {len(rows)} rows", flush=True)

            groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for r in rows:
                old_job_id, sr, co, role, fsa, lsa, st, srcs = r
                new_id = generate_job_id(sr, co, role)
                groups[(sr, new_id)].append({
                    "old": old_job_id, "new": new_id, "src_repo": sr, "co": co, "role": role,
                    "fsa": fsa, "lsa": lsa, "st": st, "srcs": srcs or [],
                })

            # Decide survivors + emit mapping rows for COPY.
            map_rows = []      # (old_id, new_id, kept)
            merge_rows = []    # (old_id_survivor, latest_lsa, union_sources_csv, new_status)
            dupe_groups = 0
            losers_total = 0
            for grp in groups.values():
                survivor = min(grp, key=lambda r: r["fsa"])
                losers = [r for r in grp if r["old"] != survivor["old"]]
                if losers:
                    dupe_groups += 1
                    losers_total += len(losers)
                    latest_lsa = max(r["lsa"] for r in grp)
                    union_srcs = sorted({s for r in grp for s in r["srcs"]})
                    any_active = any(r["st"] == "active" for r in grp)
                    new_status = "active" if any_active else "inactive"
                    merge_rows.append({
                        "survivor_old": survivor["old"],
                        "latest_lsa": latest_lsa,
                        "union_srcs": union_srcs,
                        "new_status": new_status,
                    })
                    for loser in losers:
                        map_rows.append((loser["old"], survivor["new"], False))
                map_rows.append((survivor["old"], survivor["new"], True))

            print(
                f"[dedupe_fast] groups={len(groups)} dupes={dupe_groups} "
                f"losers={losers_total} survivors_pk_swap={sum(1 for r in map_rows if r[2] and r[0]!=r[1])}",
                flush=True,
            )

            if args.dry_run:
                print("[dedupe_fast] DRY-RUN — done.", flush=True)
                return 0

            # APPLY -------------------------------------------------------------
            # Supabase pooler enforces a statement_timeout. Disable for TX.
            print("[dedupe_fast] disabling statement_timeout for this TX …", flush=True)
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute("SET LOCAL lock_timeout = 0")
            print("[dedupe_fast] dropping FK + heavy indexes + creating temp tables …", flush=True)
            cur.execute(
                "ALTER TABLE feedback DROP CONSTRAINT IF EXISTS feedback_job_id_fkey"
            )
            # The HNSW vector index is what makes UPDATE jobs SET job_id=… slow:
            # each PK rewrite is a heap delete+insert which rebuilds the HNSW node.
            # Drop now, recreate at end. content_hash index is cheap, but drop too.
            cur.execute("DROP INDEX IF EXISTS ix_jobs_embedding_hnsw")
            cur.execute("DROP INDEX IF EXISTS ix_jobs_content_hash")
            cur.execute(
                "CREATE TEMP TABLE _id_map ("
                "  old_id text PRIMARY KEY,"
                "  new_id text NOT NULL,"
                "  kept   boolean NOT NULL"
                ") ON COMMIT DROP"
            )
            cur.execute(
                "CREATE TEMP TABLE _merge ("
                "  survivor_old text PRIMARY KEY,"
                "  latest_lsa   timestamptz NOT NULL,"
                "  union_srcs   text[]      NOT NULL,"
                "  new_status   text        NOT NULL"
                ") ON COMMIT DROP"
            )

            # COPY mapping in.
            print(f"[dedupe_fast] COPY {len(map_rows)} mapping rows …", flush=True)
            buf = io.StringIO()
            for old_id, new_id, kept in map_rows:
                buf.write(f"{old_id}\t{new_id}\t{'t' if kept else 'f'}\n")
            buf.seek(0)
            with cur.copy("COPY _id_map (old_id, new_id, kept) FROM STDIN") as cp:
                cp.write(buf.read())

            print(f"[dedupe_fast] COPY {len(merge_rows)} merge rows …", flush=True)
            buf = io.StringIO()
            for m in merge_rows:
                srcs_lit = "{" + ",".join(s.replace(",", " ") for s in m["union_srcs"]) + "}"
                ts = m["latest_lsa"].isoformat()
                buf.write(f"{m['survivor_old']}\t{ts}\t{srcs_lit}\t{m['new_status']}\n")
            buf.seek(0)
            with cur.copy(
                "COPY _merge (survivor_old, latest_lsa, union_srcs, new_status) FROM STDIN"
            ) as cp:
                cp.write(buf.read())

            print("[dedupe_fast] remapping feedback …", flush=True)
            # Collision-safe approach: for each (user_id, target_new_id), keep
            # only the OLDEST feedback row (by recorded_at, tiebreak by feedback_id).
            # Then UPDATE survivor rows to point at new_id.
            cur.execute("""
                DELETE FROM feedback
                WHERE feedback_id IN (
                    SELECT feedback_id FROM (
                        SELECT fb.feedback_id,
                               row_number() OVER (
                                   PARTITION BY fb.user_id, COALESCE(m.new_id, fb.job_id)
                                   ORDER BY fb.recorded_at ASC, fb.feedback_id ASC
                               ) AS rn
                        FROM feedback fb
                        LEFT JOIN _id_map m ON fb.job_id = m.old_id
                    ) ranked
                    WHERE rn > 1
                )
            """)
            fb_collisions = cur.rowcount
            cur.execute("""
                UPDATE feedback fb
                SET job_id = m.new_id
                FROM _id_map m
                WHERE fb.job_id = m.old_id
                  AND m.old_id <> m.new_id
            """)
            fb_updated = cur.rowcount

            print("[dedupe_fast] merging survivor fields …", flush=True)
            cur.execute("""
                UPDATE jobs j
                SET last_seen_at = m.latest_lsa,
                    sources      = m.union_srcs,
                    status       = m.new_status
                FROM _merge m
                WHERE j.job_id = m.survivor_old
            """)
            merged = cur.rowcount

            print("[dedupe_fast] deleting losers …", flush=True)
            cur.execute("""
                DELETE FROM jobs j
                USING _id_map m
                WHERE j.job_id = m.old_id
                  AND m.kept = false
            """)
            deleted = cur.rowcount

            print("[dedupe_fast] swapping survivor PKs …", flush=True)
            cur.execute("""
                UPDATE jobs j
                SET job_id = m.new_id
                FROM _id_map m
                WHERE j.job_id = m.old_id
                  AND m.kept = true
                  AND m.old_id <> m.new_id
            """)
            swapped = cur.rowcount

            print("[dedupe_fast] recreating dropped indexes …", flush=True)
            cur.execute("CREATE INDEX ix_jobs_content_hash ON jobs (content_hash)")
            cur.execute(
                "CREATE INDEX ix_jobs_embedding_hnsw ON jobs "
                "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
            )
            print("[dedupe_fast] re-adding feedback FK …", flush=True)
            cur.execute(
                "ALTER TABLE feedback ADD CONSTRAINT feedback_job_id_fkey "
                "FOREIGN KEY (job_id) REFERENCES jobs(job_id)"
            )

            cur.execute("SELECT count(*) FROM jobs")
            (final_jobs,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM feedback")
            (final_fb,) = cur.fetchone()

            # Write mapping JSON BEFORE commit.
            import json, datetime as dt
            mapping_dict = {}
            for old_id, new_id, kept in map_rows:
                mapping_dict[old_id] = {"new_id": new_id, "kept": kept}
            with open("/tmp/dedupe-mapping.json", "w") as fh:
                json.dump({
                    "generatedAt": dt.datetime.utcnow().isoformat() + "Z",
                    "scanned": len(rows),
                    "groups": len(groups),
                    "deleted": deleted,
                    "swapped": swapped,
                    "merged": merged,
                    "feedback_updated": fb_updated,
                    "feedback_collisions_deleted": fb_collisions,
                    "mapping": mapping_dict,
                }, fh)

            print(
                f"[dedupe_fast] deleted={deleted}  swapped={swapped}  merged={merged}  "
                f"fb_updated={fb_updated}  fb_collisions={fb_collisions}",
                flush=True,
            )
            print(f"[dedupe_fast] final jobs={final_jobs}  feedback={final_fb}", flush=True)
            print(f"[dedupe_fast] mapping written: /tmp/dedupe-mapping.json ({len(mapping_dict)} entries)", flush=True)

            conn.commit()
            print("[dedupe_fast] COMMIT ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
