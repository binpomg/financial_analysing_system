# 研究质量输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

顶层 JSON 为 `status,summary,records,analysis,missing_data,warnings`，状态用 `completed/needs_data/not_applicable/reading_incomplete`。summary 明确报告/任务、评价范围与主要结论，不给未经定义的总分。

`records` 每项 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`。分项判断用 `record_type="assessment"`，entity 为报告或明确报告主体，period 为报告信息截止日，field 为维度名，value 为 `pass/fail/uncertain` 字符串，unit/currency 为空字符串。raw_value 可以保存被评主张原文；无单一对应语句时为空。

评价维度：任务适配与覆盖、事实与证据可靠性、论证及替代解释、专业方法与可复算性、反证风险与限制、表达与研究可用性。任务要求新增维度时说明来源，不改冻结标准含义。没有证据不得把可靠性判为 pass。

`evidence` 项 `{document_id,unit_id,quote}`；引用待评报告位置及支持判断的原始资料。不能拿其他模型评分当作原始证据。

`analysis` 每项 `{heading,text,evidence}`，对每个维度给判断理由、已确认/待核实/观点分歧、具体问题位置与影响、最小改进建议；保留有证据的优点。再给优先处理清单和评价范围限制，不使用未定义的数值排序。

`missing_data` 写原始证据、任务要求或校准资料缺口；`warnings` 写无法核验、对象不可比、专家意见分歧、模型评价未校准等与本任务实际有关的限制。对未知事实可以评价其披露透明性，不能因此肯定事实正确。
