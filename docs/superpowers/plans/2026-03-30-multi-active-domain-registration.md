# Multi-Active-Domain Registration Throughput Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-active-domain cfmail registration with multi-active-domain scheduling so throughput scales with threads while preserving CPA-backed success semantics.

**Architecture:** Refactor cfmail provisioning from single-domain rotation to an active-domain pool manager, route each worker through an explicit domain-selection channel, delete canary gating, and apply per-domain inflight/start-interval + per-domain stoploss. Keep pending token retry as a recovery sidecar, not a scheduler gate. Update dashboard/runtime APIs to expose per-domain health and throughput.

**Tech Stack:** Python 3.11, FastAPI, uv, pytest, cfmail provisioner, CPA HTTP backend.

---

## Baseline captured before execution

- Source: `http://127.0.0.1:8000/api/runtime`, `http://127.0.0.1:8000/api/summary`
- Snapshot files: `/tmp/zhuce6_baseline_runtime.json`, `/tmp/zhuce6_baseline_summary.json`
- Metrics summary is stored in the terminal log for this session.

### Task 1: T0 multi-domain provisioner primitives

**Files:**
- Modify: `core/cfmail_provisioner.py`
- Modify: `core/cfmail.py`
- Test: `tests/test_cfmail_rotation.py`

- [ ] Add failing tests for `provision_additional_domain`, `retire_domain`, and `normalize_to_domain_pool` without collapsing to one active domain.
- [ ] Run targeted pytest cases and confirm they fail for missing APIs/old single-domain behavior.
- [ ] Implement active-domain-pool operations in `CfmailProvisioner` and adjust worker domain bindings to support N active domains.
- [ ] Re-run targeted tests until green.
- [ ] Commit focused provisioner changes.

### Task 2: T1/T2 remove single-active-domain normalization and canary gating

**Files:**
- Modify: `core/registration.py`
- Modify: `core/cfmail_domain_rotation.py`
- Test: `tests/test_registration_loop.py`

- [ ] Add failing tests showing startup no longer normalizes to one domain and workers do not block on canary pending.
- [ ] Run targeted tests and confirm old behavior fails expectations.
- [ ] Delete canary pending path and replace startup normalization with domain-pool normalization.
- [ ] Re-run targeted tests until green.
- [ ] Commit scheduler bootstrap changes.

### Task 3: T3/T4 explicit domain-selection channel and per-domain throttling

**Files:**
- Modify: `core/registration.py`
- Modify: `platforms/chatgpt/plugin.py`
- Modify: `platforms/chatgpt/register.py`
- Modify: `core/cfmail.py`
- Test: `tests/test_registration_loop.py`
- Test: `tests/test_chatgpt_register.py`

- [ ] Add failing tests for worker-selected `cfmail_profile_name`, per-domain inflight tracking, and per-domain start interval.
- [ ] Run targeted tests to confirm scheduler selection path is absent.
- [ ] Implement domain scheduler state, pass selected profile through register invocation, and enforce per-domain inflight/start interval.
- [ ] Re-run targeted tests until green.
- [ ] Commit domain scheduling path.

### Task 4: T5/T6/T7 domain-level stoploss and async refill

**Files:**
- Modify: `core/registration.py`
- Modify: `core/cfmail_domain_rotation.py`
- Test: `tests/test_registration_loop.py`

- [ ] Add failing tests for `mailbox_reused`, `add_phone_gate`, and `wait_otp` causing domain retirement + async refill.
- [ ] Run targeted tests and confirm current global/single-domain behavior fails.
- [ ] Implement per-domain stoploss accounting, retirement, and background refill that does not block active workers.
- [ ] Re-run targeted tests until green.
- [ ] Commit stoploss/refill logic.

### Task 5: T8/T9/T10 mailbox entropy and recovery tuning

**Files:**
- Modify: `core/cfmail.py`
- Modify: `.env.example`
- Modify: `docs/CONFIG_REFERENCE.md`
- Test: `tests/test_base_mailbox.py`
- Test: `tests/test_registration_loop.py`

- [ ] Add failing tests for longer mailbox local parts and preserved pending-token sidecar semantics.
- [ ] Run targeted tests to verify mismatch with old local-part length / recovery expectations.
- [ ] Increase cfmail mailbox entropy and wire documented defaults for add-phone immediate recovery.
- [ ] Re-run targeted tests until green.
- [ ] Commit entropy/recovery tuning.

### Task 6: T11 proxy priority and region weighting

**Files:**
- Modify: `core/settings.py`
- Modify: `core/proxy_pool.py`
- Modify: `.env.example`
- Test: `tests/test_proxy_pool.py`
- Test: `tests/test_settings.py`

- [ ] Add failing tests for default region priority `tw,sg,jp,hk,us` and preferred-pattern ordering.
- [ ] Run targeted tests to confirm current ordering differs.
- [ ] Update defaults and any scoring needed so Taiwan/Singapore/Japan nodes dominate before fallback US nodes.
- [ ] Re-run targeted tests until green.
- [ ] Commit proxy priority changes.

### Task 7: T12 dashboard/runtime multi-domain visibility

**Files:**
- Modify: `dashboard/api.py`
- Modify: `dashboard/zhuce6.html`
- Modify: `main.py`
- Test: `tests/test_main_summary.py`

- [ ] Add failing tests for runtime/summary payloads exposing active-domain pool state and per-domain throughput/failure metrics.
- [ ] Run targeted tests to confirm payloads are missing the new fields.
- [ ] Implement API payloads and dashboard rendering for multi-domain status.
- [ ] Re-run targeted tests until green.
- [ ] Commit dashboard/runtime visibility.

### Task 8: Full verification and docs/memory closure

**Files:**
- Modify: `README.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `.codex/memory/context.md`
- Create/Update: `.codex/memory/decisions/*.md`

- [ ] Run the targeted test suite for touched files.
- [ ] Run a real runtime smoke with multi-domain config and capture before/after throughput.
- [ ] Update docs to replace single-active-domain language with active-domain-pool semantics.
- [ ] Flush memory notes describing the new scheduler and removed canary assumption.
- [ ] Commit final verification/docs/memory changes.
