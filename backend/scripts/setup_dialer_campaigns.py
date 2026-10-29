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

from app.config import get_settings
from app.database import SessionLocal
from app.models.enums import SiloName
from app.models.orm import DialerCampaign
from app.services.dialer import HARD_MAX_LIST_SIZE

# The generic per-silo loop below already creates one unfiltered
# "Silo // UCC Batch Silo" campaign (every PENDING candidate, Cold
# included) -- these three are the deliberate, non-overlapping Hot+Medium
# shards for the 3 callers actually working this batch, using the exact
# same shard mechanism two callers already split a silo with (see
# dialer.py::build_dial_queue's shard docstring). min_score is read from
# settings at creation time, not hardcoded, so it tracks whatever
# ucc_batch_medium_score_threshold is currently configured.
#
# CALLER NAME HONESTY NOTE: DialerCampaign has no co_broker/owner field --
# "Jose/Tony/James" below are campaign NAMES only, a human label for who
# works which shard, not an enforced assignment. The real attachment
# point is conversion time: apply_disposition's ADVANCE path requires a
# co_broker to credit (pipeline.convert_or_update_silo_candidate). Tony
# and James are both real CoBroker enum values already. Jose is not --
# there is no "Jose" (or house/owner) member in CoBroker today, so
# whoever runs this script's Jose shard will need either an existing
# CoBroker value to credit conversions under, or a new enum member added
# first. That's a real commission-attribution decision, not something to
# guess here -- flagging it rather than inventing a CoBroker.JOSE value.
def _ucc_batch_caller_campaigns() -> list[dict]:
    threshold = get_settings().ucc_batch_medium_score_threshold
    callers = ["Jose", "Tony", "James"]
    return [
        {
            "name": f"UCC Batch // {caller} (shard {i + 1}/{len(callers)})",
            "lead_filter": {
                "silos": [SiloName.UCC_BATCH_SILO.value],
                "min_score": threshold,
                "shard": {"of": len(callers), "index": i},
            },
            "max_attempts_per_lead": 3,
        }
        for i, caller in enumerate(callers)
    ]


# Same non-overlapping 3-way shard mechanism as the UCC Batch Silo above,
# applied to the second purchased batch (16,598 real deduped rows after
# the dry run -- see mca_history_batch.py). Capped at 500 each via
# max_list_size rather than letting the shard's natural ~5.5k/caller
# split stand -- a deliberate first-wave size smaller than even the
# existing HARD_MAX_LIST_SIZE=1000 guardrail, not a relaxation of it.
# Same CALLER NAME HONESTY NOTE as above: "Jose" is a campaign name only.
# Tony and James are real CoBroker enum values; Jose is not, so this
# shard will dial fine but nothing converted through it can be credited
# via pipeline.convert_or_update_silo_candidate until that's resolved.
def _mca_history_batch_caller_campaigns() -> list[dict]:
    assert 500 <= HARD_MAX_LIST_SIZE
    threshold = get_settings().mca_history_batch_medium_score_threshold
    callers = ["Jose", "Tony", "James"]
    return [
        {
            "name": f"MCA History Batch // {caller} (shard {i + 1}/{len(callers)}, first wave 500)",
            "lead_filter": {
                "silos": [SiloName.MCA_HISTORY_BATCH_SILO.value],
                "min_score": threshold,
                "shard": {"of": len(callers), "index": i},
            },
            "max_list_size": 500,
            "max_attempts_per_lead": 3,
        }
        for i, caller in enumerate(callers)
    ]


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
        for payload in SILO_CAMPAIGNS + STATUS_AND_CALENDAR_CAMPAIGNS + _ucc_batch_caller_campaigns() + _mca_history_batch_caller_campaigns():
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
