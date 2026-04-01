"""Shared HTTP fingerprint profile helpers for ChatGPT registration and probing."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from .constants import (
    OPENAI_SEC_CH_UA,
    OPENAI_SEC_CH_UA_MOBILE,
    OPENAI_SEC_CH_UA_PLATFORM,
    OPENAI_USER_AGENT,
)


OPENAI_FINGERPRINT_PROFILE = "chrome120_win"

_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "tw": ("tw", "taiwan", "台湾"),
    "sg": ("sg", "singapore", "新加坡"),
    "jp": ("jp", "japan", "日本"),
    "hk": ("hk", "hong kong", "香港"),
    "us": ("us", "usa", "united states", "美国"),
}


def build_browser_headers(
    *,
    access_token: str | None = None,
    account_id: str | None = None,
    accept: str = "application/json",
    content_type: str | None = "application/json",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": OPENAI_USER_AGENT,
        "sec-ch-ua": OPENAI_SEC_CH_UA,
        "sec-ch-ua-mobile": OPENAI_SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": OPENAI_SEC_CH_UA_PLATFORM,
    }
    if accept:
        headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    if extra:
        headers.update({key: value for key, value in extra.items() if value})
    return headers


def hash_device_id(device_id: str | None) -> str:
    value = str(device_id or "").strip()
    if not value:
        return ""
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def infer_proxy_region(proxy_key_or_url: str | None) -> str:
    text = str(proxy_key_or_url or "").strip().lower()
    if not text:
        return ""
    for region, aliases in _REGION_ALIASES.items():
        if any(alias in text for alias in aliases):
            return region
    return "other"


def build_registration_provenance(
    metadata: dict[str, Any] | None,
    *,
    proxy_url: str | None = None,
    proxy_key: str = "",
    proxy_region: str = "",
    cfmail_profile_name: str = "",
) -> dict[str, Any]:
    meta = dict(metadata or {})
    resolved_proxy_key = str(proxy_key or "").strip()
    resolved_proxy_url = str(proxy_url or "").strip()
    resolved_proxy_region = str(proxy_region or "").strip().lower() or infer_proxy_region(
        resolved_proxy_key or resolved_proxy_url
    )
    return {
        "registration_fingerprint_profile": OPENAI_FINGERPRINT_PROFILE,
        "registration_user_agent": OPENAI_USER_AGENT,
        "registration_sec_ch_ua": OPENAI_SEC_CH_UA,
        "registration_sec_ch_ua_mobile": OPENAI_SEC_CH_UA_MOBILE,
        "registration_sec_ch_ua_platform": OPENAI_SEC_CH_UA_PLATFORM,
        "registration_proxy_url": resolved_proxy_url,
        "registration_proxy_key": resolved_proxy_key,
        "registration_proxy_region": resolved_proxy_region,
        "registration_location": str(meta.get("location") or "").strip(),
        "registration_device_id_hash": hash_device_id(meta.get("device_id")),
        "registration_cfmail_profile_name": str(cfmail_profile_name or meta.get("cfmail_profile_name") or "").strip(),
        "registration_mail_provider": str(meta.get("mail_provider") or "").strip(),
        "registration_post_create_gate": str(meta.get("post_create_gate") or "").strip(),
        "registration_email_domain": str(meta.get("email_domain") or "").strip(),
        "registration_source": str(meta.get("source") or "").strip(),
    }
