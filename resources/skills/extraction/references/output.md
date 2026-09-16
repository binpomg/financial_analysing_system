# 提取成果约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

返回一个符合运行器业务 schema 的 JSON 对象，不添加 JSON 外说明。顶层固定为 `status, summary, records, analysis, missing_data, warnings`。

- `status`：`completed` / `needs_data` / `not_applicable` / `reading_incomplete`。`completed` 表示完成约定任务，不代表审核通过。部分结果可以随其他状态保留。
- `records` 每项包含 `record_type, entity, period, field, raw_value, value, unit, currency, evidence`，所有值字段使用字符串。`evidence` 为 `{document_id, unit_id, quote}` 数组，quote 引用对应单元的真实原文片段。表头、数值、单位、脚注分属不同单元时提供多个证据。
- `analysis` 每项为 `{heading, text, evidence}`；存放覆盖范围、字段解释、规范化公式及异常说明。`missing_data` 与 `warnings` 为字符串数组。
- 数值未知时 `value=""`；`raw_value` 原样保留破折号等原披露，原文没有披露时为空字符串。状态由 `field`/对应分析明确为“未披露、不可辨认、不适用、来源冲突”之一，不用 `0` 或不明原因的空值替代。

## 事件记录

一项事件分解为可核查字段记录，`record_type="event"`。`entity` 写出主体及角色；`field` 使用 `事件标识/字段名` 以关联同一事件，例如 `合同A/合同金额`、`合同A/签约日期`。记录原文可支持的标识，不编造公司或项目编号。`period` 表示业务日期和日期类型，不能默认等于公告日。

按任务与披露范围提取：事件类型与阶段、公告主体、履约/交易主体、对方、项目或资产、金额/股数/比例、币种单位、日期、期限、条件、变动前后状态。金额限定、联合体归属、税口径和未执行计划需单独保留。

## 表格记录

原文披露用 `record_type="observed"`，派生计算用 `"derived"`。`field` 表达完整行列语义，例如 `营业收入/分产品:产品甲/合并`；`period` 明确 `2025年度`、`2026年1—6月` 或 `2026-06-30期末`。同一指标不同期间分别记录，跨页不拆成重复数据。

归一到元、股、吨等基础单位时 `raw_value` 保留原值，`unit` 保存规范单位，分析写出原单位和倍率；保持原单位亦可，但在同一成果中保持规则一致并说明。派生数值提供全部输入证据和可重算公式。

## 必要分析条目

用 `analysis` 至少说明实际提取范围、原表/事件关系、需要解释的单位期间与缺失状态。没有相关内容时说明完整阅读后的不适用依据；不能为充实输出制造记录。
