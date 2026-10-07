# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估、关键装备质量和稳油控水协同调度。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、稳油控水方案、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/water_control/：稳油控水协同——井、层系、井组连通、测试版本、含水趋势、注采约束和平台处理上限冻结成可追溯方案，分阶段执行，异常停产或测试迟到时只重算未来时段，人工覆盖需期限和双人批准，并支持逐井解释与油水、处理能力守恒核对；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance
    PYTHONPATH=src python3 -m water_control.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成生产生产流转、油藏证据评估、装备质量和稳油控水协同流程，不访问外部网络。稳油控水验收以流花油田 32 口生产井、4 口注水井、3 个层系和 4 个连通井组为例，覆盖方案冻结、并发确认、分阶段执行、异常停产与测试迟到后的未来时段重算、人工覆盖和守恒核对。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m water_control.api --database water-control.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
