# Account Survival Upgrade Implementation Plan

## 结论

当前阶段不再把问题定义成"继续扩大指纹池".  
最新实验已经证明:

1. 注册与 probe 的 UA 指纹不一致, 确实是问题, 但已经修正.
2. 即使 `fingerprint_consistent = true`, fresh 号仍可能在几十秒内 `401 no_organization`.
3. 当前更强的根因是:
   - fresh 注册出口节点质量
   - 首用 probe 没有绑定注册代理
   - `add_phone` 成功号质量显著更差
   - 实验 cohort 混入旧号, 导致判断失真

因此, 本计划调整为**先修 fresh 首用与实验卫生, 再做长期评分与健康状态机**.

---

## 0. 约束与非目标

### 必须保持

- 保持当前主线:
  - 单池 + backend API
  - `main.py` 统一入口
  - `lite + cfmail + register`
  - `full + cpa`
  - `full + sub2api`
- 保持当前 cfmail 多活动域 runtime 语义.
- 保持当前 add_phone token 恢复链:
  - direct session token
  - workspace flow
  - fresh login fallback
  - pending retry sidecar
- 不恢复旧候选池 / 晋升池 / 两阶段池叙事.

### 这轮明确不做

- 不做浏览器自动化重写.
- 不做无限随机指纹.
- 不把 add_phone 成功号直接改成另一套后端主线.
- 不承诺"代码改了就一定长寿", 必须靠实验验证.

---

## 1. 当前阶段目标

### G1. 修正 fresh 实验样本质量

- `responses_survival` 只优先追踪带注册 provenance 的 recent accounts.
- 减少旧号污染 cohort.

### G2. 让首用尽量复用注册代理语义

- 首次 `responses` 探测优先复用:
  - `registration_proxy_key`
  - 不可用时至少复用 `registration_proxy_region`

### G3. 收紧 fresh 注册节点

- fresh 注册优先只走 `tw,sg`.
- 不再让 `us` 自然落入 fresh 主路径.

### G4. 显式标记 add_phone 风险

- 单池内新增 warmup 风险字段, 但不恢复候选池.
- add_phone 成功号进入 `warmup_required` 状态.
- survival probe 负责把它更新为:
  - `pending`
  - `passed`
  - `failed`

### G5. 为下一阶段打基础

这轮完成后, 再决定是否进入:
- cfmail domain score
- account health state machine
- bounded fingerprint profile pool

---

## 2. 实现顺序

### Task 1. Fresh 实验卫生

目标:
- `responses_survival` reseed 时优先选择:
  - recent accounts
  - 带 `registration_fingerprint_profile` 的号

文件:
- `ops/responses_survival.py`
- `core/settings.py`
- `scripts/run_responses_survival.py`
- 对应 pytest

验收:
- cohort 优先由新日志时代的 fresh 号组成
- 旧号只在无 fresh provenance 样本时才补位或被跳过

### Task 2. 首用绑定注册代理

目标:
- probe 时优先复用 `registration_proxy_key`
- 不可直接命中时, 至少按 `registration_proxy_region` 选代理

文件:
- `core/proxy_pool.py`
- `ops/responses_survival.py`
- 对应 pytest

验收:
- survival probe 对带 provenance 的账号能记录:
  - `first_use_proxy_key`
  - `first_use_proxy_region`
  - `first_invalid_proxy_key`
  - `first_invalid_proxy_region`

### Task 3. Fresh 注册区域收敛

目标:
- fresh 注册 worker 从代理池取节点时, 默认优先 `tw,sg`

文件:
- `core/settings.py`
- `core/registration.py`
- 对应 pytest

验收:
- register worker 从 proxy pool `acquire()` 时传入 fresh 区域偏好
- 设置缺失时安全回退当前行为

### Task 4. Add-phone warmup 风险标记

目标:
- 单池内显式标记 add_phone 成功号风险, 但不改变单池主线

文件:
- `platforms/chatgpt/pool.py`
- `ops/responses_survival.py`
- `dashboard/api.py`
- 对应 pytest

字段:
- `warmup_required`
- `warmup_state`
- `warmup_passed`
- `warmup_completed_at`
- `successful_probe_count`

状态:
- `not_required`
- `pending`
- `passed`
- `failed`

验收:
- add_phone 成功号默认 `warmup_required = true`
- survival 连续成功后可转 `passed`
- 首次 invalid 时可转 `failed`

### Task 5. 文档与可观测性

目标:
- 把新设置与新诊断口径写进稳定文档

文件:
- `docs/CONFIG_REFERENCE.md`
- `docs/TROUBLESHOOTING.md`

---

## 3. 这轮不做的任务

以下内容保留为下一阶段候选, 本计划不直接实现:

1. `core/cfmail_domain_score.py`
2. `ops/account_health.py`
3. bounded fingerprint profile pool
4. dashboard 大改版

原因:
- 当前最强根因不在这三块.
- 必须先用更干净的 fresh cohort 把代理与 add_phone 风险验证清楚.

---

## 4. 验收标准

### 必须满足

1. fresh cohort 优先使用带 provenance 的 recent accounts.
2. first-use / first-invalid 能带上 probe 代理信息.
3. register worker 默认优先 `tw,sg`.
4. add_phone 成功号有明确 warmup 字段.
5. targeted pytest 通过.
6. 关键回归 pytest 通过.

### 实验输出

本轮完成后要能稳定回答:

1. `tw` 与 `sg` 的寿命是否显著优于 `us`.
2. `add_phone` 组是否比非 add_phone 组更短命.
3. 首用代理与注册代理是否一致.

---

## 5. 风险

### 风险 1. strict fresh 区域偏好降低短时吞吐

接受这个风险. 当前目标是先提寿命质量, 不是继续放大低质量产号.

### 风险 2. 旧号 provenance 不完整

这轮通过 cohort 过滤减轻, 不试图补写旧历史事实.

### 风险 3. warmup 标记不等于业务隔离

这轮只做风险标记与实验分层, 不直接改变 CPA 主线语义.

---

## 6. 交付物

本轮交付物只包括:

1. revised plan
2. fresh cohort hygiene
3. proxy affinity reuse
4. fresh region preference
5. add_phone warmup metadata
6. docs update

不包含第二阶段长期评分与完整健康状态机.
