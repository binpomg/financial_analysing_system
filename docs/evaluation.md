# 评价、分区与防退化

本模块保存可检查的分项证据，不定义尚未讨论的分数权重或损失函数，也不修改稳定 Skill。所有比较结果的 `promotion_allowed` 均为 `false`。出现改善时，最多返回 `needs_review`；它表示值得进一步核实，不表示达到成熟投资研究质量或已获准升级。

## 配对比较

`finresearch.evaluation.compare_runs(baseline, candidate, regressions=None)` 接收两个完整运行记录。必需字段为 `id`、`case_id`、`line`、`partition`、`group_ids`、`skill_version`、`model`、`reasoning_effort`、`evaluation_version`、`auditor_version`、`input_digest`、`status`、`result`、`audit`。

成功运行的 `status` 为 `completed` 或 `audited`，且必须有非空结果、审核结论、问题列表、非空分项评价和审核说明。审核结构为：

```json
{
  "verdict": "pass",
  "issues": [],
  "dimensions": [
    {"name": "单位与数值", "judgment": "pass", "reason": "独立核对原值及页顶单位。"}
  ],
  "summary": "依据原件逐项核验。"
}
```

`verdict` 可为 `pass`、`revise`、`insufficient_evidence`；维度判断可为 `pass`、`fail`、`uncertain`。每个问题必须有 `severity`（`critical`／`major`／`minor`）、`category`、`description`、`evidence` 和 `correction`。同一原文位置的独立错误可以具有不同的、跨版本稳定的 `issue_id`；没有此标识时按类别、描述和证据精确匹配，措辞变化可能被保守地识别为新问题，需人工核实。

比较必须基于相同案例、材料输入指纹、文档组、分区、业务线、模型与推理配置、审核规范和评价标准。两个运行标识必须不同，Skill 版本必须不同。若记录了 `model_config`、`audit_model`、`auditor_model`、`auditor_reasoning_effort`、`auditor_config`、`tool_version`、`environment_digest` 或实际发送材料的 `material_digest`，这些字段也必须一致。输入指纹必须由任务、信息截止时点、原始材料及输出要求构成，不能包含变化中的 Skill 正文。

| 返回决定 | 含义 |
|---|---|
| `incomparable` | 缺记录、运行未完成、比较条件不同或评价范围变化 |
| `keep_baseline` | 没有改善，或出现分项／历史回归退化、新重大错误 |
| `needs_review` | 有改善信号，仍需核实审核、回归、样本充分性及预定标准 |

所有维度暂采用保守保护：已有通过退为失败或不确定，不可用其他维度改善抵消。新 `critical`／`major` 错误、同一错误严重性增加、通过结论退为需修订或证据不足均阻止升级。仅把错误严重性调低不计为改善。报告同时保留问题计数、分项前后判断、新增问题、未再报告的问题和不确定事项；没有平均分。

审核反馈是待核实证据。模型不再报告一项错误不证明错误已经解决；审核的通过结论与分项冲突也不能据此宣布通过。独立上下文和冻结规范由运行器保障，本模块只能验证收到的记录，不能证明实际的上下文隔离或全文理解。

## 历史回归与批次

`regressions` 为非空列表，每项为 `{"baseline": 旧版运行记录, "candidate": 候选运行记录}`。仅传 `passed: true` 或分数不构成证据。回归必须来自 `regression` 分区，有独立案例标识，与新批次原件组不重叠，版本、模型、工具及审核配置与本次比较一致。未提供完整回归、存在新问题、分项退化或不确定性时，`regressions.passed` 不为真。

`compare_batch(pairs, regression_pairs=None)` 接受同样形态的新批次配对及历史回归。保留所有配对，拒绝重复案例、不完整配对和混用模型／Skill 配置。任一案例的重要退化都会阻止批次采用，失败项不能从分母静默消失。传入列表之外是否有被隐藏的案例，需要由预登记批次清单核查；比较函数无法发现未交给它的数据。

这些规则不是统计显著性检验。样本量、分项容忍度、反复试验规则及晋升阈值应在正式实验前冻结；当前即使回归通过也不会产生 `promote`。最终保留集可以用于冻结版本的评测，但不能依据其反馈循环调优并继续称为未见资料。

`finresearch.experiments.run_experiment` 在任何模型调用前登记新批次与回归案例清单、全库资料组和配置指纹。所有业务线候选的开发组、已确认反馈涉及的组、以前实验登记的验证组都会排除，失败实验已经暴露的组也不能再次冒充新数据。新批次与回归不能共享原件组。中途失败保留已完成配对、失败状态和预登记范围。

正式晋升默认关闭。可选配置 `promotion_policy.enabled` 只有显式设为 `true`，同时由用户预先给出正整数 `min_validation_cases`、`min_regression_cases`、`min_improved_cases` 后才能进入正式采用检查。产品不提供这些阈值的默认数值；不认识的政策字段会报错，不会假装执行。当前已有的开发实验可以继续运行并收集证据。

政策在实验开始前连同 SHA-256 指纹保存。后续 `promote` 使用该快照，不从当前配置补政策，因此不能看完结果再打开晋升开关，也不能用一句采用理由替代门槛。采用前重新比较完整配对，核对原始运行记录、预登记案例覆盖、候选与稳定版本，要求全项可比、没有未解决不确定性、回归完整通过、候选审核通过且无重大残留，并满足冻结案例数量下限。通过后仍须明确人工核验理由，采用记录保存政策指纹与版本，旧版可恢复。比较模块本身始终不自动晋升。

问题“未再被模型报告”仍属于待核实状态，会阻止采用；不能因为满足数量下限就忽略该不确定性。这组门槛只是防止无证据升级的必要条件，不是统计显著性、真实投资可用性或人工专家校准的替代品。

## 全库分区与信息时点

`validate_partitions(cases)` 返回问题字符串列表，空列表表示提供的元数据未发现问题。跨业务线一起传入案例，才能检查全库隔离。

允许分区为 `development`／`train`、`validation`、`calibration`、`regression`、`holdout`／`test`。`holdout` 和 `test` 视为保留评测集，其原件或关联组不能和任何非保留分区重合。多个冻结业务线的保留评测可以共用原件。下一批验证是否已被以前的优化过程消费，还需运行器维护实际暴露记录，本函数不把单一 `validation` 标签视为未见证明。

读取案例顶层及 `metadata` 中的：

- `document_ids`／`doc_ids`、`group_ids`、`source_groups`／`source_group_ids`、`input_digest`；
- `documents` 列表中的原件标识、`group_id`／`group_ids`、来源组、`sha256`／`digest`、`published_at`；
- `cutoff`／`information_cutoff` 和 `published_at`。

时间必须用带显式时区的 ISO 8601，例如 `2026-09-14T16:00:00+08:00` 或 `2026-09-14T08:00:00Z`。未提供披露时间、仅给日期、未标时区或披露晚于截止时间均报告问题。不能用下载时间或文件修改时间代替披露时间。全部文档的时间记录应由材料管理模块提供；案例级披露时间不能证明未登记附件的时点。更正件、转载件、同事件、合成变体及派生任务应在登记时写入父组关系，校验器不能自行识别未登记关系。

## 金融数值核验

`check_numeric(actual, expected, actual_unit, expected_unit, tolerance="0")` 使用 `Decimal`，默认精确比较。只有显式提供时才应用绝对容差，单位是规范化后的基本单位，例如人民币元或比率。数值用字符串、整数或 `Decimal`，拒绝二进制浮点数、NaN 和无穷大。

支持人民币元／万元／亿元、股／万股／亿股、百分比与比率、百分点与基点、倍数。百分比是水平／相对比例，百分点是比例的绝对变化，二者按不同含义处理；例如 5% 不能自动等同 5 个百分点，而 0.5 个百分点等于 50 个基点。不猜币种、不做未声明汇率换算。

`check_sum(items, declared_total, tolerance="0")` 独立核对同类单位明细与合计，每项形如 `{"value": "1250.25", "unit": "万元"}`。`canonical_digest(value)` 对 JSON 内容生成稳定 SHA-256 指纹，用于冻结输入和记录；它不判断来源是否可靠。

数值相等不等于金融口径正确。主体、期间、合并／母公司、累计／单季、归母／扣非、脚注与原文支持仍由本线审核规范和原始证据检查。

验证命令：`python -m unittest discover -s test -p test_evaluation.py -v`。测试覆盖同批比较、冻结标准、重大错误、维度遗漏、历史回归、跨线泄漏、修订／合成父组、时区和未来信息、单位换算、百分点与百分比、超长十进制精度及表格合计。
