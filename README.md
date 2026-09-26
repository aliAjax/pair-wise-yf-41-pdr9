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

## 台站检修缺报的补录复核流程

台站检修期间的观测常事后补进地震事件，震级和参评台站数会随之变化，
因此补录不会直接改动已发布内容，必须经过一次独立复核才对外生效：

1. **台站下线**：`offline` 后的台站不再参与新事件关联。`associate`
   时其报告会被自动剔除，剩余在线台站报告不足两条则不允许关联。
2. **恢复后补录**：台站 `online` 恢复后，值班员（`analyst`）对
   `published`/`revised` 事件执行 `backfill`，提交补录报告、建议震级和
   原因。事件进入 `revision_pending`（待审修订）状态，已发布的
   `magnitude`、`participating_count` 保持不变；待审内容集中放在
   `data.pending_revision`，其中同时给出新旧震级、新旧参评台站（站码）
   列表、中位数振幅参考和补录所依据的基础版本号。
3. **复核确认**：复核员（`reviewer`）对照待审修订中的新旧震级和参评台站数，
   执行 `approve_revision` 且必须显式传 `"confirmed": true`，修订才对外
   发布（状态变为 `revised`，版本号递增）。值班员无权批准自己的补录；
   不确认（`confirmed:false`）会被拒绝。复核员也可 `reject_revision` 驳回，
   事件回到补录前状态，原发布内容不变，之后可以重新补录。
4. **原发布内容可查**：每次对外发布（`publish`、`revise`、
   `approve_revision`）都会保存一份不可变版本快照，通过
   `GET /api/entities/<id>/versions` 可按版本号查阅历次震级、参评台站数、
   发布人和对外通报编号；待审期间只能看到上一发布版本。
5. **全程留痕**：每个动作都写入审计记录，包含处理人（用户与角色）、UTC
   时间、状态流转（`from_status`→`to_status`）以及字段级变化
   （`detail.changes` 中每个字段的 `from`/`to`），批准时还记录新发布版本号。

事件状态流转：

```
candidate → associated → reviewed → published → revised
                              ↑  publish            ↑  revise
published/revised → revision_pending   （backfill，值班员补录）
revision_pending  → revised            （approve_revision，复核员确认）
revision_pending  → published/revised  （reject_revision，驳回回退）
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/versions`：读取事件历次对外发布的版本快照。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。

事件补录相关动作：`backfill`（值班员，参数 `station`/`magnitude`/`reason`，
可选 `time_offset`/`distance_km`/`amplitude`）、`approve_revision`
（复核员，参数 `confirmed:true`，可带新的 `communication_id`）、
`reject_revision`（复核员，参数 `reason`）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
