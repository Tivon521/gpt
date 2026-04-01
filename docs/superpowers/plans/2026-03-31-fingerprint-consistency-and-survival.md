# 2026-03-31 指纹一致与短寿命账号治理任务书

## 目标

在不改写 HTTP 注册主线的前提下, 先完成 3 件最短路径工作:

1. 指纹一致: 注册, rotate quota probe, responses survival probe 使用同一套浏览器指纹头.
2. 实验可观测: survival tracker 能按注册来源字段分组, 支持产出实验报告.
3. 日志补全: pool 记录与 survival 状态记录注册 provenance, 首次使用, 首次失效.

## 本轮实现范围

### A. 指纹一致

- 新增共享指纹模块: `/home/sophomores/zhuce6/platforms/chatgpt/fingerprint.py`
- 统一 `ops.scan` 与 `ops.rotate_probe` 的请求头到 `chrome120_win`
- 不再用 `codex_cli_rs` 或 `Codex Desktop` 作为探测 UA

### B. 短寿命保护

- 新增 `ZHUCE6_ROTATE_FRESH_GRACE_SECONDS`
- fresh 账号在 grace 窗口内跳过 rotate quota probe
- 目的: 避免刚注册成功的号立即被 rotate 打首用 API

### C. 注册 provenance

pool 文件新增:

- `registration_fingerprint_profile`
- `registration_user_agent`
- `registration_sec_ch_ua`
- `registration_proxy_url`
- `registration_proxy_key`
- `registration_proxy_region`
- `registration_device_id_hash`
- `registration_cfmail_profile_name`
- `registration_post_create_gate`
- `registration_email_domain`
- `registration_location`

### D. survival 实验字段

responses/account survival 成员新增:

- `first_use_at`
- `first_use_age_seconds`
- `first_use_fingerprint_profile`
- `fingerprint_consistent`
- `first_invalid_error_code`
- `first_invalid_error_message`
- `registration_proxy_region`
- `registration_post_create_gate`

### E. 实验报告入口

- 新增脚本: `/home/sophomores/zhuce6/scripts/survival_experiment_report.py`
- 用于按 `proxy_region`, `post_create_gate`, `fingerprint_consistency` 输出分组中位存活时长

## 验证标准

1. `ops.scan` 的 usage / responses probe 头与注册主线一致.
2. fresh 账号在 grace 窗口内不进入 rotate quota probe.
3. 新写入 pool 的注册账号带 provenance 字段.
4. survival state 中能看到 `first_use_*` 与 `first_invalid_*`.
5. `scripts/survival_experiment_report.py` 可直接从 tracker 生成分组结果.
