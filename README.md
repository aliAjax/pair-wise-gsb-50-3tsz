# 税务稽查案件与复议流程（决定版本制）

纯Python标准库实现的税务稽查案件与复议流程服务，使用SQLite持久化，HTTP接口由`http.server`提供。

## 决定版本制

补税、滞纳金和处罚金额会随证据变化，系统对每一次处理建议建立**不可变决定版本**：

- **形成建议即冻结**：`propose` 时把当时的税期、证据清单（含证据数）、补税/滞纳金/处罚/合计金额快照写入 `decision_versions`，状态为 `pending`（待确认版），案件进入 `proposed`。
- **复核确认后替换**：`review` 结论为 `accepted`（维持）或 `reduced`（按比例调减）时，待确认版转为 `current`（当前版），原当前版转为 `superseded`（旧版保留、永久可查）；结论为 `remanded` 时版本标记 `remanded`，案件退回 `investigating`，可补充资料后再次建议。
- **复议绑定当时版本**：`POST /appeals` 必须针对 `current` 版本（可显式指定 `decision_version_id`），登记记录保存绑定版本ID。期限自**决定书送达日**起算 `appeal_deadline_day`（默认60日），期内受理（案件进入 `appealed`）；逾期仍登记留痕、状态 `overdue`、写入不予受理说明，案件状态不变。
- **撤回与结案更新待办**：撤回复议案件回到 `reviewed`，审理待办完成并生成新的等待送达待办；结案结清全部待办，在审复议同步标记 `closed`。
- **页面显示**：当前版、待确认版、待办列表，并提供版本历史、复议登记记录和审计时间线。

## 模块结构（资料、规则、接口、页面各自维护）

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、金额计算、建议冻结/复核确认规则、按送达日的复议期限规则。
- `src/repository.py`：SQLite建表与事务；表 `records`、`decision_versions`、`appeals`、`todos`、`audit_events`。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：决定版本制演示页面。
- `tests/`：完整流程、规则计算、失败场景和版本制/HTTP测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：案件列表，可带`state`和`limit`。
- `GET /api/records/{id}`：案件详情。
- `GET /api/records/{id}/overview`：**总览**：当前版、待确认版、全部版本、待办（含已完成）、在审复议。
- `GET /api/records/{id}/versions`：决定版本列表（旧版可查）。
- `GET /api/records/{id}/versions/{vid}`：单个冻结版本。
- `GET /api/records/{id}/appeals`：复议登记记录（含逾期不予受理记录）。
- `GET /api/records/{id}/todos?open=1`：待办列表。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建案件，`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：业务动作，`{"expected_version":N,"data":{...}}`。
  - `investigate`（inspector）：进入调查。
  - `supplement`（inspector）：补充证据/调整数字并重算金额，状态不变。
  - `propose`（inspector）：形成建议并冻结为待确认版；data可带新的 `assessed_tax`、`evidence_count`、`evidence` 等，先重算再冻结。
  - `review`（reviewer）：`outcome=accepted|reduced|remanded`，`reduced` 可带 `reduction_pct`（默认0.5）。
  - `withdraw_appeal`（taxpayer_rep）：撤回复议。
  - `close`（reviewer）：结案。
- `POST /api/records/{id}/appeals`：登记复议（taxpayer_rep），body 的 data：
  `{"applicant","reason","served_at":"YYYY-MM-DD","applied_at":"YYYY-MM-DD","decision_version_id":可选,"expected_version":可选}`。
  响应含 `accepted`/`within_window`：`true` 为已受理；`false` 为逾期登记、不予受理（HTTP仍为200，记录可查）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`inspector`、`reviewer`、`taxpayer_rep`、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、版本冻结与替换、退回重申、逾期留痕、撤回与结案待办更新，以及HTTP冒烟。
