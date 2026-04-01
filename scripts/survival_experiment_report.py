from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any


def _bucket_key(member: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(member.get("registration_proxy_region") or "unknown").strip() or "unknown",
        str(member.get("registration_post_create_gate") or "none").strip() or "none",
        "consistent" if bool(member.get("fingerprint_consistent")) else "mismatch_or_unknown",
    )


def _median_survival_seconds(members: list[dict[str, Any]]) -> float | None:
    values = [
        int(member.get("survival_seconds"))
        for member in members
        if member.get("survival_seconds") is not None
    ]
    if not values:
        return None
    return float(median(values))


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    members = [item for item in payload.get("members") or [] if isinstance(item, dict)]
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for member in members:
        buckets.setdefault(_bucket_key(member), []).append(member)

    grouped: list[dict[str, Any]] = []
    for (proxy_region, post_create_gate, fingerprint_consistency), group_members in sorted(buckets.items()):
        invalid_members = [item for item in group_members if str(item.get("first_invalid_at") or "").strip()]
        grouped.append(
            {
                "registration_proxy_region": proxy_region,
                "registration_post_create_gate": post_create_gate,
                "fingerprint_consistency": fingerprint_consistency,
                "tracked": len(group_members),
                "invalid": len(invalid_members),
                "median_survival_seconds": _median_survival_seconds(invalid_members),
            }
        )

    return {
        "updated_at": payload.get("updated_at"),
        "probe_mode": payload.get("probe_mode"),
        "probe_fingerprint_profile": payload.get("probe_fingerprint_profile"),
        "summary": payload.get("summary") or {},
        "groups": grouped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize survival experiment buckets from tracker state")
    parser.add_argument(
        "--state-file",
        default="/home/sophomores/zhuce6/state/responses_survival_tracker.json",
        help="responses survival state file",
    )
    args = parser.parse_args()

    state_file = Path(args.state_file).expanduser().resolve()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    print(json.dumps(build_report(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
