# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（如`?status=returned`查退回配对）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录，可用`?action=pedigree_%25`按动作名过滤。
- `POST /api/imports/pedigree`：导入外部谱系，见下。
- `GET /api/imports`、`GET /api/imports/<batch_id>/rows`：导入批次与逐行断点明细。
- `GET /api/conflicts?status=open`：谱系冲突清单（页面同款数据）。
- `POST /api/conflicts/<id>/resolution`：协调员裁定冲突。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 外部谱系对账流程

导入请求体：

```json
{
  "batch_id": "partner-zoo-2026-01",
  "source": "合作园名称",
  "records": [{"id": "A001", "name": "玲玲", "sex": "female", "sire_id": "S1", "dam_id": "D1"}]
}
```

规则：

1. **按动物编号归并**：本园已有该编号则逐头对账；没有则建档并采信外部父母。
2. **父母说法冲突，两版都留**：冲突写入`pedigree_conflicts`表并在动物档案挂`open_conflicts`，本园血缘不会被覆盖。
3. **未裁定先不配对**：雌雄任一方存在未裁定冲突时，配对的`approve`直接拒绝。
4. **协调员逐条裁定**：`decision`取`keep_local`/`take_external`/`custom`（后者带`custom_parents`），仅`coordinator`/`admin`可操作。
5. **血缘改动后重算**：裁定完成后对所有`proposed`配对重算近交系数，超过红线`0.125`的配对变为`returned`待复核，退回原因写入`return_reason`与`return_history`；**已批准的结论照旧保留**。退回配对可执行`resubmit`回到待批。
6. **并发确认只放行一次**：`approve`的读取—校验—写入在同一个`BEGIN IMMEDIATE`事务内，两个协调员同时确认时后到者得到`409`。
7. **断点重试与幂等**：逐行独立事务，坏行标为`error`并记录`failed_line`，同一`batch_id`重发修正后的批次会跳过已完成行、只处理断点；批次完成后重发直接返回原结果，不重复入库。

审计分三条线留痕：动物档案事件（`pedigree_import_created/merged`、`pedigree_conflict_opened/resolved`）、导入批次事件（`pedigree_import_start/failed/complete`）、配对审批事件（`approve`、自动退回`auto_return`）。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
