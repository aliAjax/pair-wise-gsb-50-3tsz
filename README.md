# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 决定版本制

补税、滞纳金和处罚金额会随证据变化，因此处理决定按版本管理：

- **形成建议（propose）**：冻结税期、证据（数量与引用）和金额快照，生成`pending`待确认版，并产生复核待办。
- **复核（review）**：
  - `accepted`：待确认版转为`current`当前版，记录送达日，按送达日+复议期限算出届满日；
  - `reduced`：按比例重算补税、滞纳金、处罚，生成新的冻结版本成为当前版，原版本下线（`superseded`）；
  - `remanded`：待确认版作废，案件回到调查中，可再次出建议（产生新版本号）。
- 旧版本状态置为`superseded`，内容不变、继续可查；同一案件至多一个`current`、一个`pending`。
- **复议登记（appeal）**：绑定申请时的当前版本（版本号、送达日、届满日均写入登记）。申请日晚于届满日的，登记为`rejected_overdue`并写明不予受理原因，案件状态不变、记录版本不变；期限内受理后案件进入`appealed`，产生答复待办。
- **撤回复议（withdraw_appeal）**：登记置为`withdrawn`，答复待办关闭，案件回到已复核，复议窗口待办重新打开。
- **结案（close）**：关闭全部未办待办，已受理的在办复议置为`closed`。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/board`：页面聚合数据：当前版、待确认版、全部版本、复议登记和待办。
- `GET /api/records/{id}/versions`：决定版本列表（含已下线旧版）。
- `GET /api/records/{id}/appeals`：复议登记列表（含逾期不受理、撤回记录）。
- `GET /api/records/{id}/todos`：待办列表，可带`status=open|done`。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，动作包括`investigate`、`propose`、`review`、`appeal`、`withdraw_appeal`、`close`，请求体为`{"expected_version":1,"data":{...}}`。

动作数据说明：

- `propose`：`{"proposal":"...","evidence_refs":["EVD-001"]}`，冻结时校验证据数量必须大于0。
- `review`：`{"outcome":"accepted|reduced|remanded","review_note":"...","service_day":10,"reduction_pct":0.5}`，`service_day`为送达日（相对天数），确认/调减时必填为非负整数。
- `appeal`：`{"appeal_day":70,"appeal_reason":"..."}`，期限自送达日起算。
- `close`：`{"final_decision":"..."}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
