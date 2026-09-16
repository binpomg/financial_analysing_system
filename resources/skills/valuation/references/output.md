# 估值输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

顶层 JSON 为 `status,summary,records,analysis,missing_data,warnings`，状态为 `completed/needs_data/not_applicable/reading_incomplete`。summary 明确估值对象、日期、方法和完成范围。

`records` 每项 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`：`observed` 表示披露/行情输入，`assumption` 表示预测参数，`derived` 表示计算。数值保持十进制字符串；百分数 9% 保存 `"9"` 和 `%`。未知为空字符串并解释，不用零替代。

`field` 用有语义的层级标明情景和指标，如 `基准/预测营业收入`、`基准/FCFF`、`基准/企业价值`、`基准/净债务调整`、`基准/股权价值`、`基准/每股价值`。`period` 区分历史年度、预测年度和估值基准日。股数、金额与每股值的单位不能省略。

`evidence` 每项 `{document_id,unit_id,quote}`。事实引用原文；假设没有直接披露时 raw_value 为空，其说明引用支持假设的背景但不能伪称该假设已被披露。派生值引用输入来源。

`analysis` 每项 `{heading,text,evidence}`，包括：

- 方法选择、适用性与估值基准日。
- 输入清单与假设依据，明确缺失和管理层指引属性。
- 模型公式：逐期预测、现金流、折现与终值、企业/股权价值转换；写出数值代入所需变量和单位。
- 核算与工具结果：只陈述实际执行的核算；文件附件仅引用真实已生成文件。
- 场景与敏感性：说明变动参数、保持不变项和关键结果；不把多参数同变误称单因素敏感性。
- 估值区间、主要依赖、限制和失效条件。

`missing_data` 写明所需行情日期、资本结构、历史驱动或假设依据；`warnings` 记录不可比样本、受限现金、终值占比敏感等。缺关键输入时可输出模型骨架与待补清单，status 不得为 completed（除非任务明确只要求方法骨架）。
