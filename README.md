# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。

## 补录复核流程

- 台站`offline`后，其报告不再参与新事件关联：`associate`时自动剔除并记入`excluded_offline`，在线报告不足两份则关联失败。
- 台站恢复`online`后，值班员（analyst）对已发布事件执行`backfill`，提交`reason`、`magnitude`、`backfill_reports`，事件进入`revision_pending`，修订内容写入`pending_revision`（含新旧震级、参评台站数、提交人和时间）。此时原发布的震级、报告和参评台站数保持不变，仍可查询。
- 复核员（reviewer）对比`pending_revision`中的新旧值后执行`confirm_revision`，修订才对外生效：状态变为`revised`，`effective_revision`递增，`last_revision`记录本次变化；若执行`reject_revision`，事件退回补录前状态，待审修订作废。
- 补录报告中的台站必须已恢复上线，且不能重复事件已有台站。每一步的处理人、时间和变化内容都写入审计日志（`GET /api/audit`）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
