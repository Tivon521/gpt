import json
from datetime import datetime
from pathlib import Path

from ops.scan import ScanResult


def _write_token(path: Path, *, email: str, created_at: str) -> None:
    path.write_text(
        (
            "{\n"
            f'  "email": "{email}",\n'
            '  "access_token": "tok",\n'
            '  "account_id": "acct",\n'
            f'  "created_at": "{created_at}"\n'
            "}\n"
        ),
        encoding="utf-8",
    )


def _write_token_with_provenance(path: Path, *, email: str, created_at: str) -> None:
    path.write_text(
        (
            "{\n"
            f'  "email": "{email}",\n'
            '  "access_token": "tok",\n'
            '  "account_id": "acct",\n'
            f'  "created_at": "{created_at}",\n'
            '  "registration_fingerprint_profile": "chrome120_win",\n'
            '  "registration_proxy_key": "台湾原生-01",\n'
            '  "registration_proxy_region": "tw",\n'
            '  "registration_post_create_gate": "add_phone"\n'
            "}\n"
        ),
        encoding="utf-8",
    )


def test_responses_survival_seeds_recent_cohort_and_records_first_401(monkeypatch, tmp_path: Path) -> None:
    from ops.responses_survival import responses_survival_once

    newer = tmp_path / "newer@example.com.json"
    older = tmp_path / "older@example.com.json"
    _write_token(newer, email="newer@example.com", created_at="2026-03-30T20:20:00+08:00")
    _write_token(older, email="older@example.com", created_at="2026-03-30T20:19:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    def fake_probe(path, proxy, timeout):  # type: ignore[no-untyped-def]
        del proxy, timeout
        if path.name == newer.name:
            return ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok")
        return ScanResult(file=path.name, category="invalid", status_code=401, detail="unauthorized")

    monkeypatch.setattr("ops.responses_survival.probe_responses_token_file", fake_probe)

    result = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=2,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )

    assert result["probe_mode"] == "responses"
    assert result["seeded"] is True
    assert [member["email"] for member in result["members"]] == ["newer@example.com", "older@example.com"]
    assert result["summary"]["tracked"] == 2
    assert result["summary"]["alive"] == 1
    assert result["summary"]["invalid"] == 1
    invalid_member = next(member for member in result["members"] if member["email"] == "older@example.com")
    assert invalid_member["first_invalid_at"]
    assert invalid_member["survival_seconds"] is not None
    assert invalid_member["survival_seconds"] >= 0
    assert {item["to"] for item in result["changes"]} == {"normal", "invalid"}


def test_responses_survival_preserves_401_semantics_after_pool_file_is_removed(
    monkeypatch, tmp_path: Path
) -> None:
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    _write_token(tracked, email="tracked@example.com", created_at="2026-03-31T12:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="invalid", status_code=401, detail="no_organization"),
    )
    first = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )
    first_member = first["members"][0]
    assert first_member["last_probe_category"] == "invalid"
    assert first_member["state"] == "invalid"
    assert first["summary"]["invalid"] == 1
    assert first["summary"]["missing"] == 0

    tracked.unlink()
    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="missing", status_code=None, detail=f"missing_file: {path}"),
    )
    second = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
    )

    member = second["members"][0]
    assert member["last_probe_category"] == "invalid"
    assert member["state"] == "invalid_removed"
    assert member["first_invalid_at"] == first_member["first_invalid_at"]
    assert member["missing_at"]
    assert second["summary"]["invalid"] == 1
    assert second["summary"]["missing"] == 0
    assert second["summary"]["removed_after_invalid"] == 1
    assert second["changes"][-1]["from"] == "invalid"
    assert second["changes"][-1]["to"] == "invalid_removed"


def test_responses_survival_keeps_first_401_terminal_after_later_transport_error(
    monkeypatch, tmp_path: Path
) -> None:
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    _write_token(tracked, email="tracked@example.com", created_at="2026-03-31T12:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="invalid", status_code=401, detail="no_organization"),
    )
    responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="transport_error", status_code=None, detail="tls error"),
    )
    second = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
    )

    member = second["members"][0]
    assert member["last_probe_category"] == "invalid"
    assert member["state"] == "invalid"
    assert second["summary"]["invalid"] == 1
    assert second["summary"]["transport_error"] == 0


def test_responses_survival_records_registration_provenance_and_first_use_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    _write_token_with_provenance(tracked, email="tracked@example.com", created_at="2026-03-31T12:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok"),
    )

    result = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy="http://127.0.0.1:7899",
        timeout_seconds=30,
        reseed=True,
    )

    member = result["members"][0]
    assert member["registration_fingerprint_profile"] == "chrome120_win"
    assert member["registration_proxy_key"] == "台湾原生-01"
    assert member["registration_proxy_region"] == "tw"
    assert member["registration_post_create_gate"] == "add_phone"
    assert member["first_use_at"]
    assert member["first_use_age_seconds"] is not None
    assert member["first_use_fingerprint_profile"] == "chrome120_win"
    assert member["fingerprint_consistent"] is True


def test_responses_survival_prefers_recent_provenance_seed(monkeypatch, tmp_path: Path) -> None:
    from ops.responses_survival import responses_survival_once

    latest_without_provenance = tmp_path / "latest@example.com.json"
    recent_with_provenance = tmp_path / "recent@example.com.json"
    _write_token(latest_without_provenance, email="latest@example.com", created_at="2026-03-31T14:04:00+08:00")
    _write_token_with_provenance(recent_with_provenance, email="recent@example.com", created_at="2026-03-31T14:03:30+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail=str(proxy or "")),
    )

    result = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )

    assert [member["email"] for member in result["members"]] == ["recent@example.com"]


def test_responses_survival_reuses_registration_proxy_affinity(monkeypatch, tmp_path: Path) -> None:
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    _write_token_with_provenance(tracked, email="tracked@example.com", created_at="2026-03-31T12:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"
    recorded: dict[str, object] = {}

    class FakeLease:
        name = "台湾原生-01"
        proxy_url = "socks5://127.0.0.1:17891"
        local_port = 17891

    class FakePool:
        def acquire(self, timeout=5.0, preferred_name=None, preferred_regions=()):  # type: ignore[no-untyped-def]
            recorded["preferred_name"] = preferred_name
            recorded["preferred_regions"] = tuple(preferred_regions)
            return FakeLease()

        def release(self, lease, *, success, stage=None):  # type: ignore[no-untyped-def]
            recorded["released"] = (lease.name, success, stage)

    def fake_probe(path, proxy, timeout):  # type: ignore[no-untyped-def]
        recorded["proxy"] = proxy
        return ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok")

    monkeypatch.setattr("ops.responses_survival.probe_responses_token_file", fake_probe)

    result = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy="http://127.0.0.1:7899",
        timeout_seconds=30,
        reseed=True,
        proxy_pool=FakePool(),
    )

    member = result["members"][0]
    assert recorded["preferred_name"] == "台湾原生-01"
    assert recorded["preferred_regions"] == ("tw",)
    assert recorded["proxy"] == "socks5://127.0.0.1:17891"
    assert member["first_use_proxy_key"] == "台湾原生-01"
    assert member["first_use_proxy_region"] == "tw"


def test_responses_survival_marks_add_phone_warmup_passed_after_two_successful_probes(
    monkeypatch, tmp_path: Path
) -> None:
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    _write_token_with_provenance(tracked, email="tracked@example.com", created_at="2026-03-31T12:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok"),
    )

    first = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
        warmup_min_age_seconds=0,
    )
    second = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
        warmup_min_age_seconds=0,
    )

    assert first["members"][0]["warmup_state"] == "pending"
    assert second["members"][0]["warmup_state"] == "passed"
    assert second["members"][0]["warmup_passed"] is True


def test_responses_survival_requires_min_age_before_single_probe_promotion(
    monkeypatch, tmp_path: Path
) -> None:
    from ops.responses_survival import responses_survival_once

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    state_file = tmp_path / "responses_survival.json"
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    token_path = pool_dir / "fresh@example.com.json"
    token_path.write_text(
        json.dumps(
            {
                "email": "fresh@example.com",
                "access_token": "tok",
                "account_id": "acct",
                "created_at": created_at,
                "registration_post_create_gate": "add_phone",
                "warmup_required": True,
                "cpa_sync_status": "warmup_pending",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda *_args, **_kwargs: ScanResult(
            file=str(token_path),
            category="normal",
            status_code=200,
            detail="responses_ok",
        ),
    )

    result = responses_survival_once(
        pool_dir=pool_dir,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=10,
        reseed=True,
        warmup_min_age_seconds=90,
        warmup_min_successful_probes=1,
    )

    member = result["members"][0]
    assert member["successful_probe_count"] == 1
    assert member["warmup_state"] == "pending"
    assert member["warmup_passed"] is False


def test_responses_survival_promotes_passed_warmup_account_to_cpa(monkeypatch, tmp_path: Path) -> None:
    from core.settings import AppSettings
    from ops.responses_survival import responses_survival_once

    tracked = tmp_path / "tracked@example.com.json"
    tracked.write_text(
        (
            "{\n"
            '  "email": "tracked@example.com",\n'
            '  "access_token": "tok",\n'
            '  "account_id": "acct",\n'
            '  "created_at": "2026-03-31T12:00:00+08:00",\n'
            '  "registration_fingerprint_profile": "chrome120_win",\n'
            '  "registration_proxy_key": "台湾原生-01",\n'
            '  "registration_proxy_region": "tw",\n'
            '  "registration_post_create_gate": "add_phone",\n'
            '  "warmup_required": true,\n'
            '  "warmup_state": "pending",\n'
            '  "warmup_passed": false,\n'
            '  "cpa_sync_status": "warmup_pending"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "responses_survival.json"
    uploaded: list[str] = []

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok"),
    )
    monkeypatch.setattr("ops.responses_survival.get_management_key", lambda: "secret")  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "platforms.chatgpt.cpa_upload.upload_to_cpa",
        lambda token_data, api_url=None, api_key=None, proxy=None: uploaded.append(token_data["email"]) or (True, "ok"),
    )

    settings = AppSettings(cpa_management_base_url="http://127.0.0.1:8317/v0/management", backend="cpa")

    responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
        warmup_min_age_seconds=0,
        warmup_min_successful_probes=2,
        settings=settings,
    )
    second = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
        warmup_min_age_seconds=0,
        warmup_min_successful_probes=2,
        settings=settings,
    )

    assert uploaded == ["tracked@example.com"]
    assert second["members"][0]["warmup_state"] == "passed"
    assert second["promotion_stats"]["promoted_success_total"] == 1
    assert second["promotion_stats"]["promoted_failure_total"] == 0
    persisted = json.loads(tracked.read_text(encoding="utf-8"))
    assert persisted["cpa_sync_status"] == "synced"

    third = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
        warmup_min_age_seconds=0,
        warmup_min_successful_probes=2,
        settings=settings,
    )

    assert third["promotion_stats"]["promoted_success_total"] == 1
    assert third["promotion_stats"]["promoted_failure_total"] == 0


def test_responses_survival_auto_enrolls_new_warmup_member_into_existing_cohort(
    monkeypatch, tmp_path: Path
) -> None:
    from ops.responses_survival import responses_survival_once

    stable = tmp_path / "stable@example.com.json"
    _write_token(stable, email="stable@example.com", created_at="2026-03-31T18:00:00+08:00")
    state_file = tmp_path / "responses_survival.json"

    monkeypatch.setattr(
        "ops.responses_survival.probe_responses_token_file",
        lambda path, proxy, timeout: ScanResult(file=path.name, category="normal", status_code=200, detail="responses_ok"),
    )

    first = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=True,
    )
    assert [member["email"] for member in first["members"]] == ["stable@example.com"]

    pending = tmp_path / "pending@example.com.json"
    pending.write_text(
        (
            "{\n"
            '  "email": "pending@example.com",\n'
            '  "access_token": "tok",\n'
            '  "account_id": "acct",\n'
            '  "created_at": "2026-03-31T18:30:00+08:00",\n'
            '  "registration_fingerprint_profile": "chrome120_win",\n'
            '  "registration_proxy_key": "台湾原生-01",\n'
            '  "registration_proxy_region": "tw",\n'
            '  "registration_post_create_gate": "add_phone",\n'
            '  "warmup_required": true,\n'
            '  "warmup_state": "pending",\n'
            '  "warmup_passed": false,\n'
            '  "cpa_sync_status": "warmup_pending"\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    second = responses_survival_once(
        pool_dir=tmp_path,
        state_file=state_file,
        cohort_size=1,
        proxy=None,
        timeout_seconds=30,
        reseed=False,
        warmup_min_age_seconds=999999,
        warmup_min_successful_probes=2,
    )

    assert [member["email"] for member in second["members"]] == ["pending@example.com"]
    assert second["members"][0]["warmup_state"] == "pending"
    assert second["members"][0]["successful_probe_count"] == 1
