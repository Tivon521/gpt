"""Pool file helpers for zhuce6 ChatGPT token records."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import time
from typing import Any


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9@._+-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "chatgpt_account"


def build_pool_filename(token_data: dict[str, Any]) -> str:
    email = str(token_data.get("email") or "").strip()
    if email:
        return f"{_safe_component(email)}.json"
    account_id = str(token_data.get("account_id") or "").strip()
    if account_id:
        return f"{_safe_component(account_id)}.json"
    return f"chatgpt_{int(time.time())}.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _apply_pool_defaults(token_data: dict[str, Any], *, assign_created_at: bool) -> dict[str, Any]:
    payload = dict(token_data)
    post_create_gate = str(payload.get("registration_post_create_gate") or "").strip().lower()
    warmup_required = bool(payload.get("warmup_required")) or post_create_gate == "add_phone"
    payload.setdefault("health_status", "unknown")
    payload.setdefault("source", str(payload.get("source") or "register").strip() or "register")
    if assign_created_at:
        payload.setdefault("created_at", now_iso())
    else:
        payload.setdefault("created_at", "")
    payload.setdefault("backup_written", True)
    payload.setdefault("cpa_sync_status", "pending")
    payload.setdefault("last_cpa_sync_at", "")
    payload.setdefault("last_cpa_sync_error", "")
    payload.setdefault("last_probe_at", "")
    payload.setdefault("last_probe_status_code", None)
    payload.setdefault("last_probe_result", "")
    payload.setdefault("last_probe_detail", "")
    payload.setdefault("warmup_required", warmup_required)
    payload.setdefault("warmup_state", "pending" if warmup_required else "not_required")
    payload.setdefault("warmup_passed", False if warmup_required else True)
    payload.setdefault("warmup_completed_at", "")
    payload.setdefault("successful_probe_count", 0)
    payload.setdefault("first_use_proxy_key", "")
    payload.setdefault("first_use_proxy_region", "")
    payload.setdefault("first_invalid_proxy_key", "")
    payload.setdefault("first_invalid_proxy_region", "")
    payload.pop("in_main_pool", None)
    payload.pop("promoted_at", None)
    payload.pop("last_main_pool_attempted_at", None)
    return payload


def is_warmup_pending_record(payload: dict[str, Any]) -> bool:
    return bool(payload.get("warmup_required")) and not bool(payload.get("warmup_passed"))


def load_token_record(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"token record must be a JSON object: {path}")
    return _apply_pool_defaults(payload, assign_created_at=False)


def update_token_record(path: Path, **updates: Any) -> dict[str, Any]:
    payload = load_token_record(path)
    payload.update(updates)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return payload


def write_token_record(token_data: dict[str, Any], pool_dir: Path, filename: str | None = None) -> Path:
    pool_dir.mkdir(parents=True, exist_ok=True)
    target_name = filename or build_pool_filename(token_data)
    target_path = pool_dir / target_name
    payload = _apply_pool_defaults(token_data, assign_created_at=True)
    target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target_path
