"""Dashboard payload builders and runtime helpers for zhuce6."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import date, datetime, time as datetime_time
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from fastapi import FastAPI, HTTPException
except ModuleNotFoundError:
    FastAPI = Any  # type: ignore[assignment]

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

from core.paths import DEFAULT_DASHBOARD_LOG_FILE
from core.registry import list_platforms
from core.settings import AppSettings
from ops.account_survival import account_survival_once, load_account_survival_state, print_account_survival_summary
from ops.common import CpaClient, create_backend_client
from ops.responses_survival import (
    load_responses_survival_state,
    print_responses_survival_summary,
    responses_survival_once,
)
from ops.d1_cleanup import d1_cleanup_once
from ops.rotate_log import rotate_log_tail as _rotate_log_tail
from ops.service import RepeatedTask

FREE_ACCOUNT_WEEKLY_TOKENS = max(
    1,
    int(str(os.getenv("ZHUCE6_FREE_ACCOUNT_WEEKLY_TOKENS", "5000000")).strip() or "5000000"),
)
OVERVIEW_CACHE_TTL_SECONDS = 30.0


def _cleanup_once(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.cleanup import cleanup_once

    return cleanup_once(*args, **kwargs)


def _validate_once(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.validate import validate_once

    return validate_once(*args, **kwargs)


def _print_validate_summary(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.validate import print_validate_summary

    return print_validate_summary(*args, **kwargs)


def _rotate_once(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.rotate import rotate_once

    return rotate_once(*args, **kwargs)


def _print_rotate_summary(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops.rotate import print_rotate_summary

    return print_rotate_summary(*args, **kwargs)


def _fetch_validate_management_auth_files(*args, **kwargs):  # type: ignore[no-untyped-def]
    from ops import validate as validate_ops

    return validate_ops._fetch_management_auth_files(*args, **kwargs)  # type: ignore[attr-defined]


def _compat_main_attr(name: str, default: object) -> object:
    main_module = sys.modules.get("main")
    if main_module is None:
        return default
    return getattr(main_module, name, default)


def _invoke_count_cpa_files(fn: object, settings: AppSettings) -> int:
    return int(fn(settings))  # type: ignore[misc]

def _build_background_tasks(settings: AppSettings) -> list[RepeatedTask]:
    tasks: list[RepeatedTask] = []
    if settings.cleanup_enabled:
        tasks.append(
            RepeatedTask(
                "cleanup",
                lambda: _cleanup_once(
                    client=create_backend_client(settings),
                    proxy=settings.cleanup_proxy,
                    management_base_url=settings.cpa_management_base_url,
                    management_key=settings.cpa_management_key,
                    pool_dir=settings.pool_dir,
                ),
                settings.cleanup_interval,
            )
        )
    if settings.d1_cleanup_enabled:
        tasks.append(
            RepeatedTask(
                "d1_cleanup",
                lambda: d1_cleanup_once(
                    database_id=settings.d1_database_id,
                    mail_retention_hours=settings.d1_mail_retention_hours,
                    address_retention_hours=settings.d1_address_retention_hours,
                ),
                settings.d1_cleanup_interval,
            )
        )
    if settings.validate_enabled:
        tasks.append(
            RepeatedTask(
                "validate",
                lambda: _print_validate_summary(
                    _validate_once(
                        client=create_backend_client(settings),
                        proxy=settings.validate_proxy,
                        dry_run=False,
                        max_workers=settings.validate_max_workers,
                        pool_dir=settings.pool_dir,
                        scope=settings.validate_scope,
                        management_base_url=settings.cpa_management_base_url,
                        management_key=settings.cpa_management_key,
                    )
                ),
                settings.validate_interval,
            )
        )
    if settings.rotate_enabled:
        tasks.append(
            RepeatedTask(
                "rotate",
                lambda: _print_rotate_summary(
                    _rotate_once(
                        pool_dir=settings.pool_dir,
                        client=create_backend_client(settings),
                        management_base_url=settings.cpa_management_base_url,
                        cpa_management_key=settings.cpa_management_key,
                        rotate_probe_workers=settings.rotate_probe_workers,
                        fresh_grace_seconds=settings.rotate_fresh_grace_seconds,
                        cpa_runtime_reconcile_enabled=settings.cpa_runtime_reconcile_enabled,
                        cpa_runtime_reconcile_cooldown_seconds=settings.cpa_runtime_reconcile_cooldown_seconds,
                        cpa_runtime_reconcile_restart_enabled=settings.cpa_runtime_reconcile_restart_enabled,
                    )
                ),
                settings.rotate_interval,
            )
        )
    if settings.account_survival_enabled:
        tasks.append(
            RepeatedTask(
                "account_survival",
                lambda: print_responses_survival_summary(
                    responses_survival_once(
                        pool_dir=settings.pool_dir,
                        state_file=settings.responses_survival_state_file,
                        cohort_size=settings.account_survival_cohort_size,
                        proxy=settings.account_survival_proxy,
                        timeout_seconds=settings.account_survival_timeout_seconds,
                        settings=settings,
                        require_provenance=settings.responses_survival_require_provenance,
                        recent_window_seconds=settings.responses_survival_recent_window_seconds,
                        warmup_min_age_seconds=settings.warmup_min_age_seconds,
                        warmup_min_successful_probes=settings.warmup_min_successful_probes,
                    )
                ),
                settings.account_survival_interval,
            )
        )
    return tasks


def _count_pool_files(pool_dir: Path) -> int:
    if not pool_dir.is_dir():
        return 0
    try:
        return sum(1 for path in pool_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except Exception:
        return 0


def _count_cpa_files(settings: AppSettings) -> int:
    try:
        client = create_backend_client(settings)
        return len(
            [
                entry
                for entry in getattr(client, "list_auth_files")()
                if "@" in str(entry.get("name") or "").strip()
            ]
        )
    except Exception:
        return 0


def _fetch_management_auth_files(settings: AppSettings) -> tuple[bool, list[dict[str, object]]]:
    if settings.runtime_mode == "lite":
        return False, []
    try:
        client = create_backend_client(settings)
        if not getattr(client, "health_check")():
            return False, []
        files = [
            item
            for item in getattr(client, "list_auth_files")()
            if isinstance(item, dict)
        ]
    except Exception:
        return False, []
    return True, files


def _is_regular_free_account(item: dict[str, object]) -> bool:
    name = str(item.get("name") or "")
    if "@" not in name:
        return False
    id_token = item.get("id_token") or {}
    if isinstance(id_token, dict):
        plan_type = str(id_token.get("plan_type") or "").strip().lower()
        if plan_type:
            return plan_type == "free"
    return True


def _classify_regular_account_status(item: dict[str, object]) -> str | None:
    if not _is_regular_free_account(item):
        return None

    status_message = str(item.get("status_message") or "")
    unavailable = bool(item.get("unavailable"))
    lowered_status = status_message.lower()

    if "unauthorized" in lowered_status or "invalidated" in lowered_status:
        return "invalid"
    if unavailable:
        if "usage_limit_reached" in lowered_status or item.get("next_retry_after"):
            return "waiting_reset"
        return "other"
    return "available"


def _classify_regular_accounts(files: list[dict[str, object]], *, source_available: bool) -> dict[str, object]:
    stats: dict[str, object] = {
        "total": 0,
        "available": 0,
        "waiting_reset": 0,
        "invalid": 0,
        "other": 0,
        "source": "management",
        "source_available": source_available,
        "source_error": None if source_available else "management_data_unavailable",
    }

    if not source_available:
        return stats

    for item in files:
        status = _classify_regular_account_status(item)
        if status is None:
            continue
        stats["total"] = int(stats["total"]) + 1
        stats[status] = int(stats[status]) + 1
    return stats


def _estimate_tokens(regular_accounts: dict[str, object]) -> dict[str, object]:
    available = int(regular_accounts.get("available") or 0)
    waiting_reset = int(regular_accounts.get("waiting_reset") or 0)
    relevant_accounts = available + waiting_reset
    source_available = bool(regular_accounts.get("source_available"))
    return {
        "per_account": FREE_ACCOUNT_WEEKLY_TOKENS,
        "available_now": available * FREE_ACCOUNT_WEEKLY_TOKENS,
        "available_with_reset": relevant_accounts * FREE_ACCOUNT_WEEKLY_TOKENS,
        "period": "weekly",
        "estimation_mode": "count_based",
        "baseline_source": "configured",
        "relevant_accounts": relevant_accounts,
        "matched_accounts": 0,
        "weighted_accounts": 0,
        "fallback_accounts": relevant_accounts,
        "fallback_reason": None if source_available else "missing_management_inventory",
        "snapshot_timestamp": None,
        "snapshot_age_seconds": None,
        "snapshot_fresh": False,
    }


def _count_today_new(pool_dir: Path) -> int:
    if not pool_dir.is_dir():
        return 0
    try:
        today_start = datetime.combine(date.today(), datetime_time.min).timestamp()
        return sum(
            1
            for path in pool_dir.iterdir()
            if path.is_file() and path.suffix == ".json" and path.stat().st_mtime >= today_start
        )
    except Exception:
        return 0


def _dashboard_overview_payload(app: FastAPI) -> dict[str, object]:
    cache = getattr(app.state, "dashboard_overview_cache", None)
    now_monotonic = time.monotonic()
    if isinstance(cache, dict):
        created_at = float(cache.get("created_at") or 0.0)
        cached_payload = cache.get("payload")
        if now_monotonic - created_at <= OVERVIEW_CACHE_TTL_SECONDS and isinstance(cached_payload, dict):
            return cached_payload

    settings: AppSettings = app.state.settings
    runtime = _runtime_payload(app)
    register_task = next((task for task in runtime["task_states"] if task.get("name") == "register"), {})
    if settings.runtime_mode == "lite":
        cpa_count = None
        regular_accounts = None
        tokens = None
        observed_loss = None
        cpa_inventory = {
            "management_available": False,
            "count_source": "lite_mode",
            "auth_file_count": None,
        }
    else:
        fetch_management_auth_files = _compat_main_attr("_fetch_management_auth_files", _fetch_management_auth_files)
        count_cpa_files = _compat_main_attr("_count_cpa_files", _count_cpa_files)
        management_ok, auth_files = fetch_management_auth_files(settings)  # type: ignore[misc]
        cpa_count = len(auth_files) if management_ok else _invoke_count_cpa_files(count_cpa_files, settings)
        regular_accounts = _classify_regular_accounts(auth_files, source_available=management_ok)
        tokens = _estimate_tokens(regular_accounts)
        observed_loss = int(regular_accounts.get("waiting_reset") or 0) + int(regular_accounts.get("invalid") or 0)
        cpa_inventory = {
            "management_available": management_ok,
            "count_source": "backend_api" if management_ok else "api_unavailable",
            "auth_file_count": cpa_count,
        }
    total_attempts = int(register_task.get("total_attempts") or 0)
    registered_success_total = int(register_task.get("total_success_registered") or register_task.get("total_success") or 0)
    cpa_sync_success_total = int(register_task.get("total_cpa_sync_success") or 0)
    cpa_sync_failure_total = int(register_task.get("total_cpa_sync_failure") or 0)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pool_count": runtime["pool_count"],
        "cpa_count": cpa_count,
        "cpa_inventory": cpa_inventory,
        "regular_accounts": regular_accounts,
        "tokens": tokens,
        "today_new": _compat_main_attr("_count_today_new", _count_today_new)(settings.pool_dir),  # type: ignore[misc]
        "success_rate": register_task.get("success_rate") if total_attempts > 0 else None,
        "registered_success_total": registered_success_total,
        "cpa_sync_success_total": cpa_sync_success_total,
        "cpa_sync_failure_total": cpa_sync_failure_total,
        "registered_success_rate": round(registered_success_total / max(total_attempts, 1) * 100, 1) if total_attempts > 0 else None,
        "cpa_sync_success_rate": round(cpa_sync_success_total / max(total_attempts, 1) * 100, 1) if total_attempts > 0 else None,
        "burn_rate": None,
        "observed_loss": observed_loss,
    }
    app.state.dashboard_overview_cache = {
        "created_at": now_monotonic,
        "payload": payload,
    }
    return payload


def _recent_pool_files(pool_dir: Path, limit: int = 8) -> list[dict[str, object]]:
    if not pool_dir.is_dir():
        return []
    try:
        normalized_limit = max(1, int(limit))
        files = [
            path
            for path in pool_dir.iterdir()
            if path.is_file() and path.suffix == ".json"
        ]
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except Exception:
        return []
    out: list[dict[str, object]] = []
    for path in files[:normalized_limit]:
        try:
            stat = path.stat()
            out.append({
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "modified_at_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        except OSError:
            continue
    return out


def _register_log_tail(settings: AppSettings, limit: int = 80) -> dict[str, object]:
    log_path_raw = str(settings.register_log_file or "").strip()
    if not log_path_raw:
        return {
            "available": False,
            "path": "",
            "updated_at": None,
            "updated_at_iso": None,
            "error": "register log file not configured",
            "lines": [],
        }

    log_path = Path(log_path_raw).expanduser()
    if not log_path.exists():
        return {
            "available": False,
            "path": str(log_path),
            "updated_at": None,
            "updated_at_iso": None,
            "error": "register log file not found",
            "lines": [],
        }

    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = deque((line.rstrip("\r\n") for line in fh), maxlen=limit)
        stat = log_path.stat()
    except OSError as exc:
        return {
            "available": False,
            "path": str(log_path),
            "updated_at": None,
            "updated_at_iso": None,
            "error": str(exc),
            "lines": [],
        }

    return {
        "available": True,
        "path": str(log_path),
        "updated_at": stat.st_mtime,
        "updated_at_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "error": None,
        "lines": list(lines),
    }

def _runtime_state_file_meta(settings: AppSettings) -> dict[str, object]:
    state_file = Path(settings.runtime_state_file)
    if not state_file.exists():
        return {
            "exists": False,
            "path": str(state_file),
            "updated_at": None,
            "updated_at_iso": None,
        }
    stat = state_file.stat()
    return {
        "exists": True,
        "path": str(state_file),
        "updated_at": stat.st_mtime,
        "updated_at_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def _format_duration_hms(value: object) -> str | None:
    try:
        total = int(value)  # type: ignore[arg-type]
    except Exception:
        return None
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if hours > 0 or minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _attach_survival_duration_fields(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    members = result.get("members")
    if isinstance(members, list):
        enriched_members: list[dict[str, object]] = []
        for item in members:
            if not isinstance(item, dict):
                continue
            member = dict(item)
            survival_text = _format_duration_hms(member.get("survival_seconds"))
            if survival_text is not None:
                member["survival_text"] = survival_text
            enriched_members.append(member)
        result["members"] = enriched_members
    changes = result.get("changes")
    if isinstance(changes, list):
        enriched_changes: list[dict[str, object]] = []
        for item in changes:
            if not isinstance(item, dict):
                continue
            change = dict(item)
            survival_text = _format_duration_hms(change.get("survival_seconds"))
            if survival_text is not None:
                change["survival_text"] = survival_text
            enriched_changes.append(change)
        result["changes"] = enriched_changes
    return result


def _latest_fresh_unauthorized_state(state_dir: Path) -> dict[str, object]:
    candidates = sorted(
        state_dir.glob("track_new8_unauthorized*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["path"] = str(path)
            payload["updated_at_iso"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            return payload
    return {}


def _fresh_unauthorized_experiment_payload(settings: AppSettings) -> dict[str, object]:
    payload = _latest_fresh_unauthorized_state(settings.state_dir)
    if not payload:
        return {
            "available": False,
            "path": "",
            "summary": {
                "tracked": 0,
                "first_401_count": 0,
                "completed": 0,
                "pending": 0,
            },
            "members": [],
        }

    members_raw = payload.get("members")
    enriched_members: list[dict[str, object]] = []
    first_401_count = 0
    completed = 0
    if isinstance(members_raw, list):
        for item in members_raw:
            if not isinstance(item, dict):
                continue
            member = dict(item)
            first_401_text = _format_duration_hms(member.get("first_401_seconds"))
            if first_401_text is not None:
                member["first_401_text"] = first_401_text
            if str(member.get("first_401_at") or "").strip():
                first_401_count += 1
                completed += 1
            enriched_members.append(member)
    payload["members"] = enriched_members
    payload["available"] = True
    payload["summary"] = {
        "tracked": len(enriched_members),
        "first_401_count": first_401_count,
        "completed": completed,
        "pending": max(0, len(enriched_members) - completed),
    }
    return payload


def _derive_survival_promotion_stats(payload: dict[str, object]) -> dict[str, int]:
    members = payload.get("members")
    if not isinstance(members, list):
        return {
            "promoted_success_total": 0,
            "promoted_failure_total": 0,
        }
    success_total = 0
    failure_total = 0
    for item in members:
        if not isinstance(item, dict):
            continue
        path_raw = str(item.get("path") or "").strip()
        if not path_raw:
            continue
        try:
            record = json.loads(Path(path_raw).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(record, dict) or not bool(record.get("warmup_required")):
            continue
        status = str(record.get("cpa_sync_status") or "").strip().lower()
        if status == "synced":
            success_total += 1
        elif status == "failed":
            failure_total += 1
    return {
        "promoted_success_total": success_total,
        "promoted_failure_total": failure_total,
    }


def _account_survival_payload(settings: AppSettings) -> dict[str, object]:
    responses_state_file = Path(settings.responses_survival_state_file)
    responses_payload = load_responses_survival_state(responses_state_file)
    if responses_payload:
        payload = _attach_survival_duration_fields(dict(responses_payload))
        payload["promotion_stats"] = _derive_survival_promotion_stats(payload)
        payload["fresh_unauthorized_experiment"] = _fresh_unauthorized_experiment_payload(settings)
        payload["enabled"] = settings.account_survival_enabled
        payload["available"] = True
        payload["path"] = str(responses_state_file)
        payload.setdefault("probe_mode", "responses")
        return payload

    state_file = Path(settings.account_survival_state_file)
    payload = load_account_survival_state(state_file)
    if not payload:
        return {
            "enabled": settings.account_survival_enabled,
            "available": False,
            "path": str(state_file),
            "error": "account survival state file not found",
        }
    payload = _attach_survival_duration_fields(dict(payload))
    payload["fresh_unauthorized_experiment"] = _fresh_unauthorized_experiment_payload(settings)
    payload["enabled"] = settings.account_survival_enabled
    payload["available"] = True
    payload["path"] = str(state_file)
    return payload


def _responses_survival_promotion_stats(settings: AppSettings) -> dict[str, int]:
    payload = load_responses_survival_state(Path(settings.responses_survival_state_file))
    stats = payload.get("promotion_stats") if isinstance(payload, dict) else None
    if not isinstance(stats, dict):
        return {
            "promoted_success_total": 0,
            "promoted_failure_total": 0,
        }
    return {
        "promoted_success_total": int(stats.get("promoted_success_total") or 0),
        "promoted_failure_total": int(stats.get("promoted_failure_total") or 0),
    }


def _parse_runtime_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _count_runtime_warmup_promotions(settings: AppSettings, *, runtime_started_at: object) -> int:
    started_at = _parse_runtime_timestamp(runtime_started_at)
    if started_at is None or not settings.pool_dir.is_dir():
        return 0
    total = 0
    for path in settings.pool_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not bool(payload.get("warmup_required")):
            continue
        if str(payload.get("cpa_sync_status") or "").strip().lower() != "synced":
            continue
        created_at = _parse_runtime_timestamp(payload.get("created_at"))
        if created_at is None:
            try:
                created_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except Exception:
                continue
        if created_at >= started_at:
            total += 1
    return total


def _count_runtime_current_warmup_backlog(settings: AppSettings, *, runtime_started_at: object) -> int:
    started_at = _parse_runtime_timestamp(runtime_started_at)
    if started_at is None or not settings.pool_dir.is_dir():
        return 0
    total = 0
    for path in settings.pool_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("cpa_sync_status") or "").strip().lower() != "warmup_pending":
            continue
        created_at = _parse_runtime_timestamp(payload.get("created_at"))
        if created_at is None:
            try:
                created_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except Exception:
                continue
        if created_at >= started_at:
            total += 1
    return total


def _apply_warmup_promotion_metrics(task_snapshots: list[dict[str, object]], settings: AppSettings) -> list[dict[str, object]]:
    updated_snapshots: list[dict[str, object]] = []
    for snapshot in task_snapshots:
        if not isinstance(snapshot, dict) or snapshot.get("name") != "register":
            updated_snapshots.append(snapshot)
            continue
        current = dict(snapshot)
        promoted_success_total = _count_runtime_warmup_promotions(
            settings,
            runtime_started_at=current.get("last_started_at"),
        )
        total_attempts = int(current.get("total_attempts") or 0)
        direct_success_total = int(current.get("total_success_direct") or current.get("total_success_registered") or current.get("total_success") or 0)
        direct_cpa_sync_total = int(current.get("total_cpa_sync_success_direct") or current.get("total_cpa_sync_success") or 0)
        effective_success_total = direct_success_total + promoted_success_total
        effective_cpa_sync_total = direct_cpa_sync_total + promoted_success_total
        threads_total = int(current.get("threads_total") or 0)
        retry_sidecar_threads = 1 if threads_total > 0 and isinstance(current.get("pending_token_queue"), dict) else 0
        register_worker_threads = max(0, threads_total - retry_sidecar_threads)
        current_warmup_backlog = _count_runtime_current_warmup_backlog(
            settings,
            runtime_started_at=current.get("last_started_at"),
        )
        current["total_success_direct"] = direct_success_total
        current["total_success_promoted"] = promoted_success_total
        current["total_success"] = effective_success_total
        current["total_success_registered"] = effective_success_total
        current["total_cpa_sync_success_direct"] = direct_cpa_sync_total
        current["total_cpa_sync_success"] = effective_cpa_sync_total
        current["register_worker_threads"] = register_worker_threads
        current["retry_sidecar_threads"] = retry_sidecar_threads
        current["current_warmup_backlog"] = current_warmup_backlog
        current["success_rate"] = round(effective_success_total / max(total_attempts, 1) * 100, 1) if total_attempts > 0 else 0.0
        current["registered_success_rate"] = round(effective_success_total / max(total_attempts, 1) * 100, 1) if total_attempts > 0 else 0.0
        current["cpa_sync_success_rate"] = round(effective_cpa_sync_total / max(total_attempts, 1) * 100, 1) if total_attempts > 0 else 0.0
        updated_snapshots.append(current)
    return updated_snapshots


def _task_snapshots(background_tasks: list[RepeatedTask], registration_loop: RegistrationLoop | None = None) -> list[dict[str, object]]:
    snapshots = [task.snapshot() for task in background_tasks]
    if registration_loop:
        snapshots.append(registration_loop.snapshot())
    return snapshots


def _external_runtime_state(settings: AppSettings) -> dict[str, object] | None:
    state_file = Path(settings.runtime_state_file)
    if not state_file.is_file():
        return None
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _proxy_pool_payload(
    settings: AppSettings,
    registration_loop: RegistrationLoop | None = None,
) -> dict[str, object]:
    pool = getattr(registration_loop, "_proxy_pool", None) if registration_loop is not None else None
    if pool is None:
        external = _external_runtime_state(settings)
        proxy_pool = external.get("proxy_pool") if isinstance(external, dict) else None
        if isinstance(proxy_pool, dict):
            return proxy_pool
    nodes: list[dict[str, object]] = []
    snapshot_error: str | None = None
    if pool is not None:
        try:
            snapshot = pool.snapshot()
        except Exception as exc:
            snapshot_error = str(exc)
        else:
            if isinstance(snapshot, list):
                nodes = [item for item in snapshot if isinstance(item, dict)]

    return {
        "configured": bool(settings.proxy_pool_configured or pool is not None),
        "enabled": pool is not None,
        "snapshot_error": snapshot_error,
        "node_count": len(nodes),
        "in_use_count": sum(1 for item in nodes if item.get("in_use")),
        "disabled_count": sum(1 for item in nodes if item.get("disabled")),
        "nodes": nodes,
    }


def _runtime_payload(app: FastAPI) -> dict[str, object]:
    runtime_settings: AppSettings = app.state.settings
    background_tasks = getattr(app.state, "background_tasks", [])
    registration_loop = getattr(app.state, "registration_loop", None)
    task_snapshots = _task_snapshots(background_tasks, registration_loop)
    if registration_loop is None:
        external = _external_runtime_state(runtime_settings)
        register_snapshot = external.get("register_snapshot") if isinstance(external, dict) else None
        if isinstance(register_snapshot, dict):
            task_snapshots.append(register_snapshot)
    task_snapshots = _apply_warmup_promotion_metrics(task_snapshots, runtime_settings)
    return {
        "runtime_mode": runtime_settings.runtime_mode,
        "architecture": "single-process-fastapi" if registration_loop is not None else "split-runtime-fastapi+loop",
        "cleanup_enabled": runtime_settings.cleanup_enabled,
        "validate_enabled": runtime_settings.validate_enabled,
        "cleanup_interval": runtime_settings.cleanup_interval,
        "validate_interval": runtime_settings.validate_interval,
        "validate_scope": runtime_settings.validate_scope,
        "pool_dir": str(runtime_settings.pool_dir),
        "pool_count": _count_pool_files(runtime_settings.pool_dir),
        "backend": runtime_settings.backend,
        "cpa_management_base_url": runtime_settings.cpa_management_base_url,
        "account_survival_enabled": runtime_settings.account_survival_enabled,
        "account_survival_interval": runtime_settings.account_survival_interval,
        "account_survival_cohort_size": runtime_settings.account_survival_cohort_size,
        "account_survival_state_file": str(runtime_settings.account_survival_state_file),
        "rotate_enabled": runtime_settings.rotate_enabled,
        "rotate_interval": runtime_settings.rotate_interval,
        "rotate_fresh_grace_seconds": runtime_settings.rotate_fresh_grace_seconds,
        "register_fresh_proxy_regions": list(runtime_settings.register_fresh_proxy_regions),
        "responses_survival_recent_window_seconds": runtime_settings.responses_survival_recent_window_seconds,
        "responses_survival_require_provenance": runtime_settings.responses_survival_require_provenance,
        "warmup_min_age_seconds": runtime_settings.warmup_min_age_seconds,
        "warmup_min_successful_probes": runtime_settings.warmup_min_successful_probes,
        "registered_tasks": [task["name"] for task in task_snapshots],
        "task_states": task_snapshots,
        "proxy_pool": _proxy_pool_payload(runtime_settings, registration_loop),
    }


def _register_burst_plan_payload(settings: AppSettings) -> dict[str, object]:
    interval_seconds = max(60, int(settings.register_batch_interval_seconds))
    target_count = max(1, int(settings.register_batch_target_count))
    batches_per_day = max(1, math.floor(86400 / interval_seconds))
    accounts_per_day = target_count * batches_per_day
    return {
        "mode": "burst",
        "threads": max(1, int(settings.register_batch_threads)),
        "target_count": target_count,
        "interval_seconds": interval_seconds,
        "accounts_per_day": accounts_per_day,
        "accounts_needed_for_one_day_target": target_count,
        "accounts_needed_for_sustained_daily_target": max(accounts_per_day - target_count, 0),
    }


def _summary_payload(app: FastAPI) -> dict[str, object]:
    runtime = _runtime_payload(app)
    settings: AppSettings = app.state.settings
    overview = _dashboard_overview_payload(app)
    register_task = next((task for task in runtime["task_states"] if task.get("name") == "register"), {})
    rotate_task = next((task for task in runtime["task_states"] if task.get("name") == "rotate"), {})
    account_survival = _account_survival_payload(settings)
    rotate_log_tail = _compat_main_attr("_rotate_log_tail", _rotate_log_tail)()
    return {
        "project": "zhuce6",
        "generated_at": overview["generated_at"],
        "runtime": runtime,
        "platforms": list_platforms(),
        "pool_count": overview["pool_count"],
        "cpa_count": overview["cpa_count"],
        "cpa_inventory": overview["cpa_inventory"],
        "regular_accounts": overview["regular_accounts"],
        "tokens": overview["tokens"],
        "today_new": overview["today_new"],
        "success_rate": overview["success_rate"],
        "registered_success_total": overview["registered_success_total"],
        "cpa_sync_success_total": overview["cpa_sync_success_total"],
        "cpa_sync_failure_total": overview["cpa_sync_failure_total"],
        "registered_success_rate": overview["registered_success_rate"],
        "cpa_sync_success_rate": overview["cpa_sync_success_rate"],
        "burn_rate": overview["burn_rate"],
        "observed_loss": overview["observed_loss"],
        "register_failure_by_stage": register_task.get("failure_by_stage") or {},
        "register_failure_signals": register_task.get("failure_signals") or {},
        "register_recent_failure_hotspots": register_task.get("recent_failure_hotspots") or [],
        "register_recent_attempts": register_task.get("recent_attempts") or [],
        "register_cfmail_domain_pool": register_task.get("cfmail_domain_pool") or {},
        "register_cfmail_add_phone_stoploss": register_task.get("cfmail_add_phone_stoploss") or {},
        "register_cfmail_wait_otp_stoploss": register_task.get("cfmail_wait_otp_stoploss") or {},
        "register_burst_plan": _register_burst_plan_payload(settings),
        "rotate_task": rotate_task,
        "rotate_log_tail": rotate_log_tail,
        "rotate_latest_summary": rotate_log_tail.get("latest_summary"),
        "rotate_current_summary": rotate_log_tail.get("current_summary"),
        "account_survival": account_survival,
        "runtime_state_file": _runtime_state_file_meta(settings),
        "recent_pool_files": _recent_pool_files(Path(str(runtime["pool_dir"]))),
        "register_log_tail": _register_log_tail(settings),
        "routes": {
            "healthz": "/healthz",
            "platforms": "/api/platforms",
            "runtime": "/api/runtime",
            "summary": "/api/summary",
            "settings": "/api/settings",
            "health_dependencies": "/api/health/dependencies",
            "register_control": "/api/control/register",
            "account_survival": "/api/account-survival",
            "chatgpt_preflight": "/api/register/chatgpt/preflight",
            "chatgpt_register_once": "/api/register/chatgpt/run",
            "chatgpt_callback_exchange": "/api/register/chatgpt/callback-exchange",
            "zhuce6": "/zhuce6",
        },
        "commands": {
            "start": "uv run python main.py --mode full",
            "chatgpt_preflight": "uv run python scripts/chatgpt_preflight.py --json",
            "chatgpt_register_once": "uv run python scripts/chatgpt_register_once.py --json --mail-provider cfmail",
            "chatgpt_callback_exchange": "uv run python scripts/chatgpt_exchange_callback.py --json --callback-url '<url>' --state '<state>' --code-verifier '<verifier>'",
            "cleanup_once": "uv run python -m ops.cleanup --once",
            "validate_used_dry_run": "uv run python -m ops.validate --scope used --dry-run --once",
            "validate_all_dry_run": "uv run python -m ops.validate --scope all --dry-run --once --limit 20",
            "scan_local_pool": "uv run python -m ops.scan --limit 20",
            "update_priority_dry_run": "uv run python -m ops.update_priority --dry-run --limit 20",
        },
        "manual_test": [
            "Start the service and visit /zhuce6.",
            "Run scripts/chatgpt_preflight.py with a working mailbox provider and network.",
            "Run scripts/chatgpt_register_once.py with a working mailbox provider, proxy, and upstream availability if you want a full attempt.",
            "Complete the OAuth login in a browser, then run scripts/chatgpt_exchange_callback.py to write a pool file.",
            "Run ops.cleanup / ops.validate only when backend API is reachable.",
            "Live CPA invalid account cleanup remains manual_test and should be checked via quota probe plus rotate summary.",
        ],
    }

def _cpa_management_root(settings: AppSettings) -> str:
    parsed = urlsplit(settings.cpa_management_base_url)
    path = parsed.path or ""
    suffix = "/v0/management"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _settings_payload(app: FastAPI) -> dict[str, object]:
    settings: AppSettings = app.state.settings
    registration_loop = getattr(app.state, "registration_loop", None)
    missing_cfmail = settings.validate_cfmail_env()
    return {
        "mode": settings.runtime_mode,
        "register": {
            "enabled": bool(registration_loop is not None or settings.register_enabled),
            "threads": settings.register_threads,
            "batch_target_count": settings.register_batch_target_count,
            "batch_interval_seconds": settings.register_batch_interval_seconds,
            "mail_provider": settings.register_mail_provider,
            "proxy": settings.register_proxy,
            "fresh_proxy_regions": ",".join(settings.register_fresh_proxy_regions),
        },
        "proxy_pool": {
            "enabled": settings.enable_proxy_pool,
            "size": settings.proxy_pool_size,
            "config_path": str(settings.proxy_pool_config) if settings.proxy_pool_config else "",
            "direct_urls": settings.proxy_pool_direct_urls,
            "regions": ",".join(settings.proxy_pool_regions),
        },
        "cfmail": {
            "configured": len(missing_cfmail) == 0,
            "zone_name": str(os.getenv("ZHUCE6_CFMAIL_ZONE_NAME", "")).strip(),
            "worker_name": str(os.getenv("ZHUCE6_CFMAIL_WORKER_NAME", "")).strip(),
            "rotation_window": settings.cfmail_rotation_window,
            "rotation_blacklist_threshold": settings.cfmail_rotation_blacklist_threshold,
        },
        "cpa": {
            "configured": settings.runtime_mode != "lite" and settings.backend == "cpa",
            "backend": settings.backend,
            "management_url": _cpa_management_root(settings),
            "rotate_enabled": settings.rotate_enabled,
            "rotate_interval": settings.rotate_interval,
            "rotate_fresh_grace_seconds": settings.rotate_fresh_grace_seconds,
        },
        "survival": {
            "recent_window_seconds": settings.responses_survival_recent_window_seconds,
            "require_provenance": settings.responses_survival_require_provenance,
            "warmup_min_age_seconds": settings.warmup_min_age_seconds,
            "warmup_min_successful_probes": settings.warmup_min_successful_probes,
        },
    }


def _encode_env_value(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if any(ch.isspace() for ch in text) or "#" in text:
        return json.dumps(text)
    return text


def _persist_env_updates(path: Path, updates: dict[str, object]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    normalized_updates = {key: _encode_env_value(value) for key, value in updates.items()}
    handled: set[str] = set()
    output_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        candidate = stripped[7:] if stripped.startswith("export ") else stripped
        key, sep, _value = candidate.partition("=")
        if sep and key in normalized_updates:
            if key in handled:
                continue
            output_lines.append(f"{key}={normalized_updates[key]}")
            handled.add(key)
            continue
        output_lines.append(line)
    for key, value in normalized_updates.items():
        if key not in handled:
            output_lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


def _parse_settings_patch(changes: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    updates: dict[str, object] = {}
    env_updates: dict[str, object] = {}

    def parse_regions(value: object) -> tuple[str, ...]:
        return tuple(part.strip().lower() for part in str(value or "").split(",") if part.strip())

    allowed: dict[str, tuple[str, str, object]] = {
        "register.threads": ("register_threads", "ZHUCE6_REGISTER_THREADS", lambda value: max(1, int(value))),
        "register.batch_target_count": (
            "register_batch_target_count",
            "ZHUCE6_REGISTER_BATCH_TARGET_COUNT",
            lambda value: max(1, int(value)),
        ),
        "register.batch_interval_seconds": (
            "register_batch_interval_seconds",
            "ZHUCE6_REGISTER_BATCH_INTERVAL_SECONDS",
            lambda value: max(60, int(value)),
        ),
        "register.mail_provider": (
            "register_mail_provider",
            "ZHUCE6_REGISTER_MAIL_PROVIDER",
            lambda value: str(value or "").strip() or "cfmail",
        ),
        "register.proxy": ("register_proxy", "ZHUCE6_REGISTER_PROXY", lambda value: str(value or "").strip()),
        "register.fresh_proxy_regions": (
            "register_fresh_proxy_regions",
            "ZHUCE6_REGISTER_FRESH_PROXY_REGIONS",
            parse_regions,
        ),
        "proxy_pool.size": ("proxy_pool_size", "ZHUCE6_PROXY_POOL_SIZE", lambda value: max(1, int(value))),
        "proxy_pool.direct_urls": (
            "proxy_pool_direct_urls",
            "ZHUCE6_PROXY_POOL_DIRECT_URLS",
            lambda value: str(value or "").strip(),
        ),
        "proxy_pool.regions": ("proxy_pool_regions", "ZHUCE6_PROXY_POOL_REGIONS", parse_regions),
        "cpa.rotate_interval": ("rotate_interval", "ZHUCE6_ROTATE_INTERVAL", lambda value: max(1, int(value))),
        "cpa.rotate_fresh_grace_seconds": (
            "rotate_fresh_grace_seconds",
            "ZHUCE6_ROTATE_FRESH_GRACE_SECONDS",
            lambda value: max(0, int(value)),
        ),
        "survival.recent_window_seconds": (
            "responses_survival_recent_window_seconds",
            "ZHUCE6_RESPONSES_SURVIVAL_RECENT_WINDOW_SECONDS",
            lambda value: max(0, int(value)),
        ),
        "survival.require_provenance": (
            "responses_survival_require_provenance",
            "ZHUCE6_RESPONSES_SURVIVAL_REQUIRE_PROVENANCE",
            lambda value: str(value or "").strip().lower() in {"1", "true", "yes", "on"},
        ),
        "survival.warmup_min_age_seconds": (
            "warmup_min_age_seconds",
            "ZHUCE6_WARMUP_MIN_AGE_SECONDS",
            lambda value: max(0, int(value)),
        ),
        "survival.warmup_min_successful_probes": (
            "warmup_min_successful_probes",
            "ZHUCE6_WARMUP_MIN_SUCCESSFUL_PROBES",
            lambda value: max(1, int(value)),
        ),
    }

    for key, value in changes.items():
        spec = allowed.get(key)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unsupported setting: {key}")
        field_name, env_name, parser = spec
        parsed_value = parser(value)
        updates[field_name] = parsed_value
        if isinstance(parsed_value, tuple):
            env_updates[env_name] = ",".join(str(item) for item in parsed_value)
        else:
            env_updates[env_name] = parsed_value
    return updates, env_updates


def _cfmail_dependency_payload(settings: AppSettings) -> dict[str, object]:
    if "cfmail" not in {part.strip() for part in settings.register_mail_provider.split(",") if part.strip()}:
        return {"status": "unconfigured", "detail": "register_mail_provider_not_cfmail"}
    missing = settings.validate_cfmail_env()
    if missing:
        return {"status": "unconfigured", "detail": f"missing: {', '.join(missing)}"}
    return {"status": "ok", "detail": "configuration_present"}


def _proxy_pool_dependency_payload(app: FastAPI) -> dict[str, object]:
    settings: AppSettings = app.state.settings
    if not settings.enable_proxy_pool:
        return {"status": "unconfigured", "detail": "proxy_pool_disabled", "active_nodes": 0, "total_nodes": 0}
    if not settings.proxy_pool_configured:
        return {"status": "unconfigured", "detail": "proxy_pool_not_configured", "active_nodes": 0, "total_nodes": 0}
    proxy_pool = _proxy_pool_payload(settings, getattr(app.state, "registration_loop", None))
    total_nodes = int(proxy_pool.get("node_count") or 0)
    active_nodes = max(0, total_nodes - int(proxy_pool.get("disabled_count") or 0))
    snapshot_error = str(proxy_pool.get("snapshot_error") or "").strip()
    if snapshot_error:
        return {
            "status": "error",
            "detail": snapshot_error,
            "active_nodes": active_nodes,
            "total_nodes": total_nodes,
        }
    return {
        "status": "ok" if total_nodes > 0 else "error",
        "detail": "ok" if total_nodes > 0 else "no_proxy_nodes",
        "active_nodes": active_nodes,
        "total_nodes": total_nodes,
    }


def _cpa_dependency_payload(settings: AppSettings) -> dict[str, object]:
    if settings.runtime_mode == "lite":
        return {
            "status": "unconfigured",
            "management_reachable": False,
        }
    if settings.backend == "sub2api":
        return {
            "status": "unconfigured",
            "management_reachable": False,
        }
    management_reachable = False
    try:
        management_reachable = CpaClient.from_settings(settings).health_check()
    except Exception:
        management_reachable = False
    return {
        "status": "ok" if management_reachable else "error",
        "management_reachable": management_reachable,
    }


def _sub2api_dependency_payload(settings: AppSettings) -> dict[str, object]:
    if settings.runtime_mode == "lite":
        return {"status": "unconfigured", "error": "lite_mode", "auth_configured": False}
    if settings.backend != "sub2api":
        return {"status": "unconfigured", "error": "backend_cpa", "auth_configured": False}
    auth_configured = bool(settings.sub2api_api_key or (settings.sub2api_admin_email and settings.sub2api_admin_password))
    if not auth_configured:
        return {"status": "error", "error": "missing_auth", "auth_configured": False}
    reachable = False
    try:
        reachable = bool(create_backend_client(settings).health_check())
    except Exception:
        reachable = False
    return {
        "status": "ok" if reachable else "error",
        "error": None if reachable else "unreachable",
        "auth_configured": True,
        "base_url": settings.sub2api_base_url,
    }

build_background_tasks = _build_background_tasks
count_pool_files = _count_pool_files
count_cpa_files = _count_cpa_files
fetch_management_auth_files = _fetch_management_auth_files
is_regular_free_account = _is_regular_free_account
classify_regular_account_status = _classify_regular_account_status
classify_regular_accounts = _classify_regular_accounts
estimate_tokens = _estimate_tokens
count_today_new = _count_today_new
dashboard_overview_payload = _dashboard_overview_payload
recent_pool_files = _recent_pool_files
register_log_tail = _register_log_tail
runtime_state_file_meta = _runtime_state_file_meta
account_survival_payload = _account_survival_payload
task_snapshots = _task_snapshots
external_runtime_state = _external_runtime_state
proxy_pool_payload = _proxy_pool_payload
runtime_payload = _runtime_payload
register_burst_plan_payload = _register_burst_plan_payload
summary_payload = _summary_payload
settings_payload = _settings_payload
cpa_management_root = _cpa_management_root
encode_env_value = _encode_env_value
persist_env_updates = _persist_env_updates
parse_settings_patch = _parse_settings_patch
cfmail_dependency_payload = _cfmail_dependency_payload
proxy_pool_dependency_payload = _proxy_pool_dependency_payload
cpa_dependency_payload = _cpa_dependency_payload
sub2api_dependency_payload = _sub2api_dependency_payload
