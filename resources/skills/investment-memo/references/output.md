# 投资备忘录输出约定

运行器要求外层 `{coverage,result}` 时，下文 JSON 约定描述其中的 `result`；必须同时返回运行器要求的逐单元 `coverage`，不可省略或虚报。

返回 JSON：`status,summary,records,analysis,missing_data,warnings`。status 为 `completed/needs_data/not_applicable/reading_incomplete`；summary 写明对象、研究截止日、期限、论证摘要与实际完成范围。

`records` 每项 `{record_type,entity,period,field,raw_value,value,unit,currency,evidence}`。事实 `observed`、估值/财务计算 `derived`、研究假设 `assumption`、跟踪项目可用 `monitoring`。数字与其他值均为字符串，未知为空字符串并说明原因；百分数以百分数数字和 `%` 保存。

`evidence` 项为 `{document_id,unit_id,quote}`；假设引用背景证据并明确假设性质，没有直接原文时 raw_value 为空，不伪装披露。日期区分截止日、财务期间和预期事件窗口。

`analysis` 项 `{heading,text,evidence}`，按任务范围包括：

1. 研究问题、期限、信息时点和限制。
2. 商业模式与关键事实：利润/现金来源、竞争与资本配置。
3. 核心投资命题：证据→机制→价值影响→必要条件。
4. 预期与差异：有市场基准才称预期差，无基准则明确缺失。
5. 估值与场景：方法、输入、公式、结果、假设和敏感项。
6. 反方论证与风险：证据、触发机制、可能后果。
7. 催化与跟踪：条件、观察指标、来源、预期窗口和失效条件。
8. 研究结论与待核实：与任务约束和证据强度相称。

各分析中明确“事实/计算/假设/观点”。`missing_data` 列完成关键论证所需原件、估值输入或用户约束；`warnings` 保存不可验证市场预期、时点冲突、资料依赖和无法定量处。不能以全部标题存在代替内容完成。
