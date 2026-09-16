# 研报纠错输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

JSON 顶层 `status,summary,records,analysis,missing_data,warnings`；status 使用 `completed/needs_data/not_applicable/reading_incomplete`。`completed` 可包括待核实项，但若任务要求的关键核查因缺证据无法完成，应为 needs_data。

每条记录 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`：

- `record_type="issue"` 表示已确认错误；`"unverified"` 表示待核实；如任务要求主张台账，可用 `"checked_claim"` 保存已核查无实质错误项。
- `field` 标识报告位置/主张，例如 `报告第3页图2/2025营业收入`；`raw_value` 保存报告原主张；`value` 为有证据支持的修正文本或数字，不能确认则空字符串并解释。
- `entity` 是主张主体，`period` 是主张所涉期间或基准日，unit/currency 与数值相符；非数值字段可以为空字符串。
- `evidence` 为 `{document_id,unit_id,quote}` 数组。确认错误通常同时引用报告原主张和相反原始证据；计算错误引用报告公式及真实输入，说明独立计算。

`analysis` 元素 `{heading,text,evidence}`，为问题编号提供：核实状态、错误类别、严重性与理由、报告定位、正确依据、最小修正及影响位置。严重性使用文字 `critical/major/minor`，不是数字评分；有分歧时描述分歧，不强行定错。

另用分析说明任务覆盖、检查过的主张类别和未完成范围。无错误时无需制造 issue，但仍说明检查范围和原件支持。`missing_data` 写所缺来源与待核实主张；`warnings` 保存证据冲突、时点限制和无法查证之处。
