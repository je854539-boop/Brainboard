"""One-shot import of the MCA History Batch Silo's purchased contact
list (.xlsx or .csv) into SiloCandidate rows -- staged, not counted
toward brain.py's threshold until a caller promotes one off the dialer
(see app/services/mca_history_batch.py's module docstring for why).

Run from the VPS, inside the running api container, after the first
deploy (alembic upgrade head must have run already):
    docker compose exec api python scripts/import_mca_history_batch.py /path/to/list.xlsx

Or locally against a dev DB:
    cd backend && python import_mca_history_batch.py /path/to/list.xlsx

Idempotent in spirit, not in fact: re-running against the same file
re-dedupes against whatever's already in Master Log V2 and skips
in-batch duplicates again, but does NOT skip rows already imported as
SiloCandidate from a prior run of this script (there's no uniqueness
constraint on company_name there) -- don't run this twice against the
same file without checking the silo grid first.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.services import mca_history_batch


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python scripts/import_mca_history_batch.py /path/to/list.xlsx")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"error: no such file: {path}")
        sys.exit(1)

    with open(path, "rb") as f:
        content = f.read()

    db = SessionLocal()
    try:
        result = mca_history_batch.import_mca_history_batch_file(db, content, os.path.basename(path))
    finally:
        db.close()

    if "error" in result:
        print(f"import failed: {result['error']}")
        sys.exit(1)

    print(f"imported: {result['imported']}")
    print(f"duplicates skipped (already in Master Log V2 or repeated in this file): {result['duplicates_skipped']}")
    print(f"blank business_name skipped: {result['blank_name_skipped']}")
    print(f"dismissed on revenue floor: {result['dismissed_on_revenue_floor']}")
    print(f"tier counts: {result['tier_counts']}")
    print()
    print("Next: push these to the separate sheet (once GOOGLE_MCA_HISTORY_BATCH_SHEET_ID is set) via")
    print("sheets_sync.push_mca_history_batch_candidates, or work them straight off the Targets page.")


if __name__ == "__main__":
    main()
