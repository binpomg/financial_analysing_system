# 产业链输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

JSON 顶层为 `status,summary,records,analysis,missing_data,warnings`，状态为 `completed/needs_data/not_applicable/reading_incomplete`。其他状态可附已完成局部内容。

`records` 每项 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`。已披露供需、价格、产能与暴露用 `observed`，假设用 `assumption`，情景计算用 `derived`；值使用字符串，未知为空字符串且说明原因。百分数直接保存百分数数字与 `%` 单位。

`entity` 写公司/产品/区域与角色，`period` 指明时点/期间与计划或实际；`field` 明确价格类别、产能状态或情景，例如 `现货含税报价/产品甲`、`情景上行/单位毛利变化`。每条证据为 `{document_id,unit_id,quote}`，不引用其他业务线的未核实结论。

`analysis` 元素 `{heading,text,evidence}`，按任务包括：事件事实与时点、产业关系、逐条传导路径、公司实际暴露、时滞和抵消条件、情景计算、反证与跟踪指标。路径 text 明确“起点→中间变量→终点，方向，前提，时滞，反证”；方向可以为不确定，不能强选利好或利空。

情景计算写完整公式、输入单位和假设，`raw_value` 对假设留空，不伪称原文披露。敏感参数缺少证据时保留为待核实假设，不能偷偷使用行业常识数值。

`missing_data` 列关系、公司暴露或执行条件所缺资料及用途；`warnings` 说明市场价格与公司价格差异、聚合口径、后续数据不可用、可能重复计算等。
