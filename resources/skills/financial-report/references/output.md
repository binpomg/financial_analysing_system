# 财务分析输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

返回单一 JSON，字段为 `status,summary,records,analysis,missing_data,warnings`。状态使用 `completed/needs_data/not_applicable/reading_incomplete`；完成局部任务时在 summary 明确范围。

每条 `records` 为 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`；值均为字符串，未知值为空字符串并明确原因。`record_type` 为 `observed` 或 `derived`，事实指标与计算指标分开。`field` 携带报表范围及指标，如 `合并/营业收入`、`合并/经营现金流净额`、`归母/扣非净利润`。`period` 写出累计、单季或期末性质。百分数 12.5% 用 value `"12.5"` 与 unit `"%"`。

`evidence` 数组元素为 `{document_id,unit_id,quote}`。原始值引用原文，派生指标引用全部输入；`analysis` 用 `{heading,text,evidence}` 说明公式、口径、舍入和解释。没有依据的精确数值不输出。

根据任务范围组织以下分析条目：

- 研究对象、信息截止日与可比性调整。
- 经营表现与量价成本归因；注明不能分解的部分。
- 盈利质量：持续性、非经常项目、会计估计和重要税费影响。
- 现金质量：利润与现金差异、营运资本及一次性来源。
- 资产负债与审计风险：到期压力、受限资产、减值及审计事项。
- 反证、替代解释与后续核实事项。

事实、计算、推断在 text 中明确标记。`missing_data` 列具体所缺原件/期间/指标及其影响；`warnings` 保存不可比、后续更正、取数冲突等。未提供多期数据时可以输出单期事实，但不能把完整多期分析标为已完成。
