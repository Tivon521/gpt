"""Fixed cohort survival tracking using the codex responses API."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import time
from pathlib import Path
from typing import Any

from core.proxy_pool import ProxyLease, ProxyPool
from core.settings import AppSettings
from ops.common import get_management_key
from platforms.chatgpt.fingerprint import OPENAI_FINGERPRINT_PROFILE
from platforms.chatgpt.constants import OPENAI_USER_AGENT
from platforms.chatgpt.pool import load_token_record, update_token_record

from .scan import (
    ScanResult,
    _extract_credentials,
    _load_token_payload,
    _probe_responses_path,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _duration_seconds(started_at: str, ended_at: str) -> int | None:
    start_dt = _parse_iso(started_at)
    end_dt = _parse_iso(ended_at)
    if start_dt is None or end_dt is None:
        return None
    return max(0, int((end_dt - start_dt).total_seconds()))


def _member_age_seconds(member: dict[str, Any], probed_at: str) -> int | None:
    return _duration_seconds(str(member.get("created_at") or ""), probed_at)


def _compact_text(value: str, limit: int = 320) -> str:
    return " ".join(str(value or "").split())[:limit]


def _extract_error_facts(detail: str) -> tuple[str, str]:
    raw = str(detail or "").strip()
    if not raw:
        return "", ""
    try:
        payload = json.loads(raw)
    except Exception:
        return "", raw[:160]
    error = payload.get("error")
    if not isinstance(error, dict):
        return "", raw[:160]
    return str(error.get("code") or "").strip(), str(error.get("message") or "").strip()[:160]


def _state_template(
    *,
    pool_dir: Path,
    cohort_size: int,
    proxy: str | None,
    timeout_seconds: int,
    seed_source: str = "latest_generated_pool_files",
) -> dict[str, Any]:
    return {
        "probe_mode": "responses",
        "probe_target": "codex_responses",
        "updated_at": "",
        "seeded_at": "",
        "seed_source": seed_source,
        "pool_dir": str(pool_dir),
        "cohort_size": max(1, int(cohort_size)),
        "proxy": str(proxy or "").strip() or None,
        "probe_fingerprint_profile": OPENAI_FINGERPRINT_PROFILE,
        "probe_user_agent": OPENAI_USER_AGENT,
        "timeout_seconds": max(5, int(timeout_seconds)),
        "members": [],
        "summary": {
            "tracked": 0,
            "alive": 0,
            "invalid": 0,
            "missing": 0,
            "removed_after_invalid": 0,
            "transport_error": 0,
            "suspicious": 0,
            "never_probed": 0,
            "first_invalid_count": 0,
        },
        "promotion_stats": {
            "promoted_success_total": 0,
            "promoted_failure_total": 0,
            "last_promoted_at": "",
        },
        "changes": [],
        "round_count": 0,
    }


def load_responses_survival_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _persist_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _seed_member(path: Path) -> dict[str, Any] | None:
    try:
        payload = load_token_record(path)
    except Exception:
        return None
    email = str(payload.get("email") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    account_id = str(payload.get("account_id") or "").strip()
    if not email or not access_token or not account_id:
        return None
    created_at = str(payload.get("created_at") or "").strip()
    if not created_at:
        created_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    return {
        "email": email,
        "file_name": path.name,
        "path": str(path),
        "created_at": created_at,
        "selected_at": now_iso(),
        "first_probe_at": "",
        "last_probe_at": "",
        "probe_count": 0,
        "last_probe_status_code": None,
        "last_probe_category": "",
        "last_probe_detail": "",
        "transport_error_count": 0,
        "suspicious_count": 0,
        "missing_at": "",
        "removed_after_invalid_at": "",
        "last_missing_detail": "",
        "first_invalid_at": "",
        "first_invalid_error_code": "",
        "first_invalid_error_message": "",
        "first_use_at": "",
        "first_use_age_seconds": None,
        "first_use_fingerprint_profile": "",
        "fingerprint_consistent": None,
        "registration_fingerprint_profile": str(payload.get("registration_fingerprint_profile") or "").strip(),
        "registration_proxy_key": str(payload.get("registration_proxy_key") or "").strip(),
        "registration_proxy_region": str(payload.get("registration_proxy_region") or "").strip(),
        "registration_post_create_gate": str(payload.get("registration_post_create_gate") or "").strip(),
        "warmup_required": bool(payload.get("warmup_required")) or str(payload.get("registration_post_create_gate") or "").strip().lower() == "add_phone",
        "warmup_state": str(payload.get("warmup_state") or "").strip()
        or ("pending" if str(payload.get("registration_post_create_gate") or "").strip().lower() == "add_phone" else "not_required"),
        "warmup_passed": bool(payload.get("warmup_passed")) or str(payload.get("warmup_state") or "").strip() == "passed",
        "warmup_completed_at": str(payload.get("warmup_completed_at") or "").strip(),
        "successful_probe_count": int(payload.get("successful_probe_count") or 0),
        "warmup_promotion_recorded": bool(payload.get("warmup_promotion_recorded")),
        "warmup_promotion_result": str(payload.get("warmup_promotion_result") or "").strip(),
        "first_use_proxy_key": str(payload.get("first_use_proxy_key") or "").strip(),
        "first_use_proxy_region": str(payload.get("first_use_proxy_region") or "").strip(),
        "first_invalid_proxy_key": str(payload.get("first_invalid_proxy_key") or "").strip(),
        "first_invalid_proxy_region": str(payload.get("first_invalid_proxy_region") or "").strip(),
        "survival_seconds": None,
        "state": "tracking",
    }


def _collect_seed_members(
    pool_dir: Path,
    *,
    require_provenance: bool = False,
    recent_window_seconds: int = 0,
) -> list[dict[str, Any]]:
    recent_with_provenance: list[tuple[float, dict[str, Any]]] = []
    recent_without_provenance: list[tuple[float, dict[str, Any]]] = []
    cutoff_seconds = max(0, int(recent_window_seconds or 0))
    cutoff_ts = time.time() - cutoff_seconds if cutoff_seconds > 0 else 0.0
    for path in pool_dir.glob("*.json"):
        if not path.is_file():
            continue
        member = _seed_member(path)
        if member is None:
            continue
        created_at = _parse_iso(str(member.get("created_at") or ""))
        sort_ts = created_at.timestamp() if created_at is not None else path.stat().st_mtime
        if cutoff_ts and sort_ts < cutoff_ts:
            continue
        has_provenance = bool(str(member.get("registration_fingerprint_profile") or "").strip())
        if has_provenance:
            recent_with_provenance.append((sort_ts, member))
        elif not require_provenance:
            recent_without_provenance.append((sort_ts, member))
    recent_with_provenance.sort(key=lambda item: item[0], reverse=True)
    recent_without_provenance.sort(key=lambda item: item[0], reverse=True)
    ordered = [member for _ts, member in recent_with_provenance]
    ordered.extend(member for _ts, member in recent_without_provenance)
    return ordered


def _seed_members(
    pool_dir: Path,
    cohort_size: int,
    *,
    require_provenance: bool = False,
    recent_window_seconds: int = 0,
) -> list[dict[str, Any]]:
    ordered = _collect_seed_members(
        pool_dir,
        require_provenance=require_provenance,
        recent_window_seconds=recent_window_seconds,
    )
    return ordered[: max(1, int(cohort_size))]


def _member_identity(member: dict[str, Any]) -> str:
    return str(member.get("path") or member.get("file_name") or member.get("email") or "").strip()


def _member_created_ts(member: dict[str, Any]) -> float:
    created = _parse_iso(str(member.get("created_at") or "").strip())
    if created is not None:
        return created.timestamp()
    path = Path(str(member.get("path") or "")).expanduser()
    if path.is_file():
        return path.stat().st_mtime
    return 0.0


def _is_pending_warmup_member(member: dict[str, Any]) -> bool:
    return bool(member.get("warmup_required")) and not bool(member.get("warmup_passed")) and not _member_has_invalid_history(member)


def _merge_member_state(existing: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fresh)
    merged.update(existing)
    merged["selected_at"] = str(existing.get("selected_at") or fresh.get("selected_at") or now_iso())
    return merged


def _member_priority(member: dict[str, Any]) -> tuple[float, float]:
    created_ts = _member_created_ts(member)
    if _is_pending_warmup_member(member):
        return (0.0, created_ts)
    last_probe = _parse_iso(str(member.get("last_probe_at") or "").strip())
    last_probe_ts = last_probe.timestamp() if last_probe is not None else 0.0
    if _member_has_invalid_history(member):
        return (1.0, -last_probe_ts)
    if str(member.get("last_probe_at") or "").strip():
        return (2.0, -last_probe_ts)
    return (3.0, -created_ts)


def _refresh_active_members(
    pool_dir: Path,
    members: list[dict[str, Any]],
    *,
    cohort_size: int,
    require_provenance: bool = False,
    recent_window_seconds: int = 0,
) -> list[dict[str, Any]]:
    candidates = _collect_seed_members(
        pool_dir,
        require_provenance=require_provenance,
        recent_window_seconds=recent_window_seconds,
    )
    candidate_by_key = {_member_identity(member): member for member in candidates if _member_identity(member)}
    existing_keys: set[str] = set()
    combined: list[dict[str, Any]] = []

    for raw_member in members:
        if not isinstance(raw_member, dict):
            continue
        key = _member_identity(raw_member)
        if key:
            existing_keys.add(key)
        fresh = candidate_by_key.get(key)
        combined.append(_merge_member_state(raw_member, fresh) if isinstance(fresh, dict) else raw_member)

    for candidate in candidates:
        key = _member_identity(candidate)
        if not key or key in existing_keys:
            continue
        combined.append(candidate)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in sorted(combined, key=_member_priority):
        key = _member_identity(member)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(member)
        if len(deduped) >= max(1, int(cohort_size)):
            break
    return deduped


def probe_responses_token_file(path: Path, proxy: str | None, timeout_seconds: int) -> ScanResult:
    payload, load_error = _load_token_payload(path)
    if load_error is not None:
        return load_error
    assert payload is not None
    credentials = _extract_credentials(path, payload)
    if isinstance(credentials, ScanResult):
        return credentials
    access_token, account_id = credentials
    return _probe_responses_path(path, access_token, account_id, proxy, timeout_seconds)


def _member_outcome(member: dict[str, Any]) -> str:
    state = str(member.get("state") or "").strip()
    if state == "invalid_removed":
        return "invalid_removed"
    category = str(member.get("last_probe_category") or "").strip()
    return category or "never_probed"


def _member_has_invalid_history(member: dict[str, Any]) -> bool:
    state = str(member.get("state") or "").strip()
    category = str(member.get("last_probe_category") or "").strip()
    return bool(str(member.get("first_invalid_at") or "").strip()) or state in {"invalid", "invalid_removed"} or category == "invalid"


def _preserve_terminal_invalid(member: dict[str, Any]) -> None:
    if str(member.get("last_probe_category") or "").strip() != "invalid":
        member["last_probe_category"] = "invalid"
    if member.get("last_probe_status_code") in {None, ""}:
        member["last_probe_status_code"] = 401
    detail = str(member.get("last_probe_detail") or "").strip()
    if not detail or detail.startswith("missing_file:"):
        member["last_probe_detail"] = "invalid_before_pool_removal"


def _persist_member_fields(member: dict[str, Any]) -> None:
    path = Path(str(member.get("path") or "")).expanduser()
    if not path.is_file():
        return
    try:
        update_token_record(
            path,
            warmup_required=bool(member.get("warmup_required")),
            warmup_state=str(member.get("warmup_state") or "").strip(),
            warmup_passed=bool(member.get("warmup_passed")),
            warmup_completed_at=str(member.get("warmup_completed_at") or "").strip(),
            successful_probe_count=int(member.get("successful_probe_count") or 0),
            warmup_promotion_recorded=bool(member.get("warmup_promotion_recorded")),
            warmup_promotion_result=str(member.get("warmup_promotion_result") or "").strip(),
            first_use_at=str(member.get("first_use_at") or "").strip(),
            first_use_age_seconds=member.get("first_use_age_seconds"),
            first_use_proxy_key=str(member.get("first_use_proxy_key") or "").strip(),
            first_use_proxy_region=str(member.get("first_use_proxy_region") or "").strip(),
            first_invalid_at=str(member.get("first_invalid_at") or "").strip(),
            first_invalid_error_code=str(member.get("first_invalid_error_code") or "").strip(),
            first_invalid_error_message=str(member.get("first_invalid_error_message") or "").strip(),
            first_invalid_proxy_key=str(member.get("first_invalid_proxy_key") or "").strip(),
            first_invalid_proxy_region=str(member.get("first_invalid_proxy_region") or "").strip(),
            survival_seconds=member.get("survival_seconds"),
        )
    except Exception:
        return


def _maybe_promote_warmup_member(
    member: dict[str, Any],
    *,
    settings: AppSettings | None,
    probed_at: str,
) -> str | None:
    if settings is None or str(settings.backend or "").strip().lower() != "cpa":
        return None
    if not bool(member.get("warmup_required")) or not bool(member.get("warmup_passed")):
        return None
    path = Path(str(member.get("path") or "")).expanduser()
    if not path.is_file():
        return None
    try:
        payload = load_token_record(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("cpa_sync_status") or "").strip().lower() == "synced":
        return None
    key = str(get_management_key() or "").strip()
    if not key:
        update_token_record(
            path,
            cpa_sync_status="failed",
            last_cpa_sync_at=probed_at,
            last_cpa_sync_error="CPA management key unavailable",
        )
        member["cpa_sync_status"] = "failed"
        return "failed"
    api_url = str(settings.cpa_management_base_url or "").strip()
    if api_url.endswith("/v0/management"):
        api_url = api_url[: -len("/v0/management")]
    from platforms.chatgpt.cpa_upload import upload_to_cpa

    ok, message = upload_to_cpa(payload, api_url=api_url.rstrip("/"), api_key=key, proxy=None)
    if ok:
        update_token_record(
            path,
            health_status="good",
            cpa_sync_status="synced",
            last_cpa_sync_at=probed_at,
            last_cpa_sync_error="",
        )
        member["cpa_sync_status"] = "synced"
        return "success"
    else:
        update_token_record(
            path,
            cpa_sync_status="failed",
            last_cpa_sync_at=probed_at,
            last_cpa_sync_error=message,
        )
        member["cpa_sync_status"] = "failed"
        return "failed"


def _update_member(
    member: dict[str, Any],
    result: ScanResult,
    probed_at: str,
    *,
    probe_proxy_key: str = "",
    probe_proxy_region: str = "",
    warmup_min_age_seconds: int = 600,
    warmup_min_successful_probes: int = 2,
) -> dict[str, Any]:
    previous_outcome = _member_outcome(member)
    member["last_probe_at"] = probed_at
    if not str(member.get("first_probe_at") or "").strip():
        member["first_probe_at"] = probed_at
    if not str(member.get("first_use_at") or "").strip():
        member["first_use_at"] = probed_at
        member["first_use_age_seconds"] = _duration_seconds(
            str(member.get("created_at") or "").strip(),
            probed_at,
        )
        member["first_use_fingerprint_profile"] = OPENAI_FINGERPRINT_PROFILE
        member["first_use_proxy_key"] = str(probe_proxy_key or "").strip()
        member["first_use_proxy_region"] = str(probe_proxy_region or "").strip()
        registration_profile = str(member.get("registration_fingerprint_profile") or "").strip()
        if registration_profile:
            member["fingerprint_consistent"] = registration_profile == OPENAI_FINGERPRINT_PROFILE
    member["probe_count"] = int(member.get("probe_count") or 0) + 1

    if result.category == "missing" and _member_has_invalid_history(member):
        if not str(member.get("missing_at") or "").strip():
            member["missing_at"] = probed_at
        if not str(member.get("removed_after_invalid_at") or "").strip():
            member["removed_after_invalid_at"] = probed_at
        member["last_missing_detail"] = _compact_text(result.detail or "")
        _preserve_terminal_invalid(member)
        member["state"] = "invalid_removed"
        next_outcome = _member_outcome(member)
        detail = _compact_text(
            f"removed_after_invalid | {member.get('last_missing_detail') or ''}"
        )
        return {
            "email": str(member.get("email") or "").strip(),
            "from": previous_outcome,
            "to": next_outcome,
            "probed_at": probed_at,
            "survival_seconds": member.get("survival_seconds"),
            "detail": detail,
        }

    if result.category != "invalid" and _member_has_invalid_history(member):
        member["post_invalid_probe_at"] = probed_at
        member["post_invalid_probe_category"] = result.category
        member["post_invalid_probe_detail"] = _compact_text(result.detail or "")
        _preserve_terminal_invalid(member)
        member["state"] = "invalid"
        next_outcome = _member_outcome(member)
        return {
            "email": str(member.get("email") or "").strip(),
            "from": previous_outcome,
            "to": next_outcome,
            "probed_at": probed_at,
            "survival_seconds": member.get("survival_seconds"),
            "detail": member["post_invalid_probe_detail"],
        }

    member["last_probe_status_code"] = result.status_code
    member["last_probe_category"] = result.category
    member["last_probe_detail"] = _compact_text(result.detail or "")

    if result.category == "transport_error":
        member["transport_error_count"] = int(member.get("transport_error_count") or 0) + 1
    elif result.category not in {"normal", "invalid", "missing"}:
        member["suspicious_count"] = int(member.get("suspicious_count") or 0) + 1
    elif result.category == "missing" and not str(member.get("missing_at") or "").strip():
        member["missing_at"] = probed_at

    if result.category == "invalid":
        if not str(member.get("first_invalid_at") or "").strip():
            member["first_invalid_at"] = probed_at
            member["survival_seconds"] = _duration_seconds(
                str(member.get("created_at") or "").strip() or str(member.get("first_probe_at") or "").strip(),
                probed_at,
            )
            error_code, error_message = _extract_error_facts(result.detail or "")
            member["first_invalid_error_code"] = error_code
            member["first_invalid_error_message"] = error_message
            member["first_invalid_proxy_key"] = str(probe_proxy_key or "").strip()
            member["first_invalid_proxy_region"] = str(probe_proxy_region or "").strip()
        if bool(member.get("warmup_required")):
            member["warmup_state"] = "failed"
            member["warmup_passed"] = False
        member["state"] = "invalid"
    elif result.category == "missing":
        member["state"] = "missing"
    else:
        if result.category == "normal":
            member["successful_probe_count"] = int(member.get("successful_probe_count") or 0) + 1
            if bool(member.get("warmup_required")) and not bool(member.get("warmup_passed")):
                current_age = _member_age_seconds(member, probed_at)
                if (
                    int(member.get("successful_probe_count") or 0) >= max(1, int(warmup_min_successful_probes))
                    and (current_age is not None and int(current_age) >= max(0, int(warmup_min_age_seconds)))
                ):
                    member["warmup_passed"] = True
                    member["warmup_state"] = "passed"
                    member["warmup_completed_at"] = probed_at
        member["state"] = "tracking"
    _persist_member_fields(member)

    next_outcome = _member_outcome(member)
    return {
        "email": str(member.get("email") or "").strip(),
        "from": previous_outcome,
        "to": next_outcome,
        "probed_at": probed_at,
        "survival_seconds": member.get("survival_seconds"),
        "detail": member["last_probe_detail"],
    }


def _probe_member_proxy(
    member: dict[str, Any],
    *,
    default_proxy: str | None,
    proxy_pool: ProxyPool | None,
) -> tuple[str | None, str, str, ProxyLease | None]:
    preferred_name = str(member.get("registration_proxy_key") or "").strip()
    preferred_region = str(member.get("registration_proxy_region") or "").strip().lower()
    if proxy_pool is None:
        return default_proxy, preferred_name, preferred_region, None
    try:
        lease = proxy_pool.acquire(
            timeout=5.0,
            preferred_name=preferred_name or None,
            preferred_regions=(preferred_region,) if preferred_region else (),
        )
    except Exception:
        return default_proxy, preferred_name, preferred_region, None
    return lease.proxy_url, lease.name, preferred_region or "", lease


def _build_summary(members: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "tracked": len(members),
        "alive": 0,
        "invalid": 0,
        "missing": 0,
        "removed_after_invalid": 0,
        "transport_error": 0,
        "suspicious": 0,
        "never_probed": 0,
        "first_invalid_count": 0,
    }
    for member in members:
        outcome = _member_outcome(member)
        if outcome == "never_probed":
            summary["never_probed"] += 1
        elif outcome == "normal":
            summary["alive"] += 1
        elif outcome == "invalid":
            summary["invalid"] += 1
        elif outcome == "invalid_removed":
            summary["invalid"] += 1
            summary["removed_after_invalid"] += 1
        elif outcome == "missing":
            summary["missing"] += 1
        elif outcome == "transport_error":
            summary["transport_error"] += 1
        else:
            summary["suspicious"] += 1
        if str(member.get("first_invalid_at") or "").strip():
            summary["first_invalid_count"] += 1
    return summary


def responses_survival_once(
    *,
    pool_dir: Path,
    state_file: Path,
    cohort_size: int,
    proxy: str | None,
    timeout_seconds: int,
    reseed: bool = False,
    proxy_pool: ProxyPool | None = None,
    require_provenance: bool = False,
    recent_window_seconds: int = 0,
    warmup_min_age_seconds: int = 600,
    warmup_min_successful_probes: int = 2,
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    state = load_responses_survival_state(state_file)
    seeded = False
    reseeded = False

    if not state or reseed:
        state = _state_template(
            pool_dir=pool_dir,
            cohort_size=cohort_size,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        state["members"] = _seed_members(
            pool_dir,
            int(state.get("cohort_size") or cohort_size),
            require_provenance=require_provenance,
            recent_window_seconds=recent_window_seconds,
        )
        state["seeded_at"] = now_iso()
        seeded = True
        reseeded = reseed
    else:
        state.setdefault("probe_mode", "responses")
        state.setdefault("probe_target", "codex_responses")
        state.setdefault("pool_dir", str(pool_dir))
        state.setdefault("cohort_size", max(1, int(cohort_size)))
        state.setdefault("proxy", str(proxy or "").strip() or None)
        state.setdefault("probe_fingerprint_profile", OPENAI_FINGERPRINT_PROFILE)
        state.setdefault("probe_user_agent", OPENAI_USER_AGENT)
        state.setdefault("timeout_seconds", max(5, int(timeout_seconds)))
        state.setdefault("members", [])
        state.setdefault("summary", {})
        state.setdefault("changes", [])
        state.setdefault(
            "promotion_stats",
            {
                "promoted_success_total": 0,
                "promoted_failure_total": 0,
                "last_promoted_at": "",
            },
        )
        state.setdefault("seed_source", "latest_generated_pool_files")
        state.setdefault("round_count", 0)

    if not isinstance(state.get("members"), list):
        state["members"] = []
    if not isinstance(state.get("promotion_stats"), dict):
        state["promotion_stats"] = {
            "promoted_success_total": 0,
            "promoted_failure_total": 0,
            "last_promoted_at": "",
        }
    if not state["members"]:
        state["members"] = _seed_members(
            pool_dir,
            int(state.get("cohort_size") or cohort_size),
            require_provenance=require_provenance,
            recent_window_seconds=recent_window_seconds,
        )
        state["seeded_at"] = now_iso()
        seeded = True
    else:
        state["members"] = _refresh_active_members(
            pool_dir,
            [member for member in state["members"] if isinstance(member, dict)],
            cohort_size=int(state.get("cohort_size") or cohort_size),
            require_provenance=require_provenance,
            recent_window_seconds=recent_window_seconds,
        )

    changes: list[dict[str, Any]] = []
    for raw_member in state["members"]:
        if not isinstance(raw_member, dict):
            continue
        member = raw_member
        probed_at = now_iso()
        selected_proxy, probe_proxy_key, probe_proxy_region, lease = _probe_member_proxy(
            member,
            default_proxy=str(state.get("proxy") or "").strip() or None,
            proxy_pool=proxy_pool,
        )
        try:
            result = probe_responses_token_file(
                Path(str(member.get("path") or "")),
                selected_proxy,
                max(5, int(state.get("timeout_seconds") or timeout_seconds)),
            )
        finally:
            if lease is not None and proxy_pool is not None:
                try:
                    proxy_pool.release(lease, success=result.category == "normal" if 'result' in locals() else None, stage=result.category if 'result' in locals() else None)
                except Exception:
                    pass
        change = _update_member(
            member,
            result,
            probed_at,
            probe_proxy_key=probe_proxy_key,
            probe_proxy_region=probe_proxy_region,
            warmup_min_age_seconds=warmup_min_age_seconds,
            warmup_min_successful_probes=warmup_min_successful_probes,
        )
        promotion_outcome = _maybe_promote_warmup_member(
            member,
            settings=settings,
            probed_at=probed_at,
        )
        if promotion_outcome in {"success", "failed"} and not bool(member.get("warmup_promotion_recorded")):
            stats = state["promotion_stats"]
            member["warmup_promotion_recorded"] = True
            member["warmup_promotion_result"] = promotion_outcome
            if promotion_outcome == "success":
                stats["promoted_success_total"] = int(stats.get("promoted_success_total") or 0) + 1
            else:
                stats["promoted_failure_total"] = int(stats.get("promoted_failure_total") or 0) + 1
            stats["last_promoted_at"] = probed_at
            _persist_member_fields(member)
        if change["from"] != change["to"]:
            changes.append(change)

    state["updated_at"] = now_iso()
    state["summary"] = _build_summary([member for member in state["members"] if isinstance(member, dict)])
    state["changes"] = changes
    state["seeded"] = seeded
    state["reseeded"] = reseeded
    state["state_file"] = str(state_file)
    state["round_count"] = int(state.get("round_count") or 0) + 1
    _persist_state(state_file, state)
    return state


def print_responses_survival_summary(result: dict[str, Any]) -> None:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    print(
        "[responses-survival] summary | "
        f"tracked={int(summary.get('tracked') or 0)} | alive={int(summary.get('alive') or 0)} "
        f"| invalid={int(summary.get('invalid') or 0)} | missing={int(summary.get('missing') or 0)} "
        f"| removed_after_invalid={int(summary.get('removed_after_invalid') or 0)} "
        f"| transport_error={int(summary.get('transport_error') or 0)} "
        f"| suspicious={int(summary.get('suspicious') or 0)}"
    )
    for change in result.get("changes") or []:
        if not isinstance(change, dict):
            continue
        survival_seconds = change.get("survival_seconds")
        survival_text = f" | survival={survival_seconds}s" if survival_seconds is not None else ""
        print(
            f"[responses-survival] state change | {change.get('email') or '?'} | "
            f"{change.get('from') or 'never_probed'} -> {change.get('to') or '?'}{survival_text}"
        )
    print(f"[responses-survival] state={result.get('state_file') or '-'}")


def run_responses_survival_loop(
    *,
    pool_dir: Path,
    state_file: Path,
    cohort_size: int,
    proxy: str | None,
    timeout_seconds: int,
    interval_seconds: int,
    reseed: bool = False,
    max_rounds: int = 0,
    settings: AppSettings | None = None,
) -> dict[str, Any]:
    round_index = 0
    last_result: dict[str, Any] = {}
    proxy_pool = ProxyPool.from_settings(settings) if settings is not None else None
    if proxy_pool is not None:
        proxy_pool.start()
    while True:
        round_index += 1
        last_result = responses_survival_once(
            pool_dir=pool_dir,
            state_file=state_file,
            cohort_size=cohort_size,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
            reseed=reseed and round_index == 1,
            proxy_pool=proxy_pool,
            require_provenance=bool(settings.responses_survival_require_provenance) if settings is not None else False,
            recent_window_seconds=int(settings.responses_survival_recent_window_seconds) if settings is not None else 0,
            warmup_min_age_seconds=int(settings.warmup_min_age_seconds) if settings is not None else 600,
            warmup_min_successful_probes=int(settings.warmup_min_successful_probes) if settings is not None else 2,
            settings=settings,
        )
        print_responses_survival_summary(last_result)
        if max_rounds > 0 and round_index >= max_rounds:
            if proxy_pool is not None:
                proxy_pool.close()
            return last_result
        time.sleep(max(5, int(interval_seconds)))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run responses survival tracking for recently created accounts.")
    parser.add_argument("--pool-dir", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--cohort-size", type=int, default=8)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--max-rounds", type=int, default=0, help="0 means run forever")
    parser.add_argument("--reseed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    run_responses_survival_loop(
        pool_dir=Path(args.pool_dir).expanduser().resolve(),
        state_file=Path(args.state_file).expanduser().resolve(),
        cohort_size=max(1, int(args.cohort_size)),
        proxy=str(args.proxy or "").strip() or None,
        timeout_seconds=max(5, int(args.timeout_seconds)),
        interval_seconds=max(5, int(args.interval_seconds)),
        reseed=bool(args.reseed),
        max_rounds=max(0, int(args.max_rounds)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
