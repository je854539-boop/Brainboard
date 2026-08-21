"""One-shot setup: creates the standard set of dialer campaigns this
brokerage actually works from -- one per silo (so pending candidates can
be screened/called before conversion, same "real vs. garbage" workflow
as the Targets page) plus status-based campaigns for the manual pipeline
stages that benefit from a call (Ghosted win-back, Chase Docs follow-up,
New Lead first-touch) and a Calendar Follow-Up campaign for anything with
a follow-up date due.

Idempotent -- skips any campaign whose name already exists, so it's safe
to re-run (e.g. after a fresh deploy) without creating duplicates. Does
NOT attach a number pool or caller_connect_number -- those are real
SignalWire numbers/phones you provision and wire up per-campaign
afterward (dialer.html's "New Campaign" panel, or DialerCampaignUpdate
via the API), since this script has no way to know what numbers you
actually have.

Run with:
    cd backend && python3 scripts/setup_dialer_campaigns.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.enums import SiloName
from app.models.orm import DialerCampaign

SILO_CAMPAIGNS = [{"name": f"Silo // {silo.value}", "lead_filter": {"silos": [silo.value]}, "max_attempts_per_lead": 3} for silo in SiloName]

STATUS_AND_CALENDAR_CAMPAIGNS = [
    {"name": "Status // Ghosted (win-back)", "lead_filter": {"master_log_statuses": ["Ghosted"]}, "max_attempts_per_lead": 2},
    {"name": "Status // Chase Docs", "lead_filter": {"master_log_statuses": ["Chase Docs"]}, "max_attempts_per_lead": 3},
    {"name": "Status // New Lead", "lead_filter": {"master_log_statuses": ["New lead"]}, "max_attempts_per_lead": 3},
    {"name": "Calendar Follow-Up", "lead_filter": {"calendar": True}, "max_attempts_per_lead": 3},
]


def main() -> None:
    db = SessionLocal()
    try:
        existing_names = {name for (name,) in db.query(DialerCampaign.name).all()}
        created, skipped = 0, 0
        for payload in SILO_CAMPAIGNS + STATUS_AND_CALENDAR_CAMPAIGNS:
            if payload["name"] in existing_names:
                print(f"skip (already exists): {payload['name']}")
                skipped += 1
                continue
            db.add(DialerCampaign(**payload))
            print(f"created: {payload['name']}  filter={payload['lead_filter']}")
            created += 1
        db.commit()
        print(f"\n{created} created, {skipped} already existed.")
        print("Next: attach a number pool + caller_connect_number to each campaign you're ready to run.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
