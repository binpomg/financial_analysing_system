# sanhao：工作台与 FinAgent 的合并

本分支将 `ayhnencac/financial_analysing_system` 的 FinAgent 实现整合到原金融研究工作台。来源是 `3de42b1c9db0b5d6ee8d0d8fa8ef56c79a47a114`（平台）及 `169eb2630fa7b1b6bccf3ed800985d039e286693`（集成），保留原作者 ayhnencac 的贡献说明。采用整理后的代码合入，不导入包含原件压缩包、图片及个人机器记录的 fork 历史。

## 实际执行结构

```text
原 finresearch CLI / Web / pipeline / campaign
  → finagent.workbench
      → index_sources：引用工作台原件，登记来源、解析候选、创建图像 OCR 待办
      → research_and_audit：冻结本线 Skill，完整读取材料，独立执行业务与模型审核
  → 原有 JSON / Markdown / CSV 成果与 Web 展示
```

原 `finresearch run`、网页研究按钮和连续批次已共同经过 FinAgent 工作流，研究结果包含关联工作流 ID、节点状态和候选数量。原始文件只在工作台 `raw/` 中保存一份，FinAgent 按完整 SHA-256 引用它；旧工作台的文档 ID、案例、资料组、时间戳、分区、Skill 版本及导出接口继续有效。历史案例在首次运行时补登记，无须批量改写历史结果。

默认模型研究仍使用已冻结的 `resources/skills/` 和独立审核规范。fork 的七条专项 Flow、规范快照、公式、候选审核、冲突决议、公开采集和样例评测均已合入，可通过下述 `agent` 入口使用。专项流程尚未证明优于原稳定方法，因此本次没有自动晋升或替换稳定 Skill。规则 reviewer 的 `pass` 仅表示规则检查通过，不等同于模型独立审核或专家确认。

## 安装和统一配置

完整源码 checkout，使用 Python 3.12：

```text
git clone --branch sanhao https://github.com/binpomg/financial_analysing_system.git
cd financial_analysing_system
python -m venv .venv
```

在激活的虚拟环境中：

```text
python -m pip install -r requirements.dev.lock.txt
python -m pytest test tests -q
python -m finresearch doctor
python -m finresearch serve --port 8765
```

`requirements.lock.txt` 安装基础运行依赖；`requirements.dev.lock.txt` 额外安装测试、PDF 表格和 OCR Python 适配器。Tesseract 引擎及语言包需要本机另行提供，代码不依赖作者的工具路径。

FinAgent 默认复用 `FIN_API_BASE_URL`、`FIN_API_KEY` 和 `config.json` 的模型、推理强度、协议、容量及脱敏传输。按 [使用说明](usage.md) 的隐藏输入方式配置。默认不会读取其他应用的凭据，不会发起模型请求。原 fork 的四个 `FINAGENT_MODEL_*` 变量作为显式覆盖保留，必须四个一起提供，部分覆盖会报错；只影响专项入口，不会改写工作台配置。

## 四类使用场景

1. **只有财报原件**：导入 PDF，登记真实发布时间，创建财报分析案例，选择 `financial-report`。原页面图像和辅助文本完整交给业务与审核请求；表格解析候选单独保存，缺少历史报表或市场信息时保留缺口。
2. **一批公司资料**：逐个导入并组成同一个案例，使用 `run CASE_ID --all-lines`。七线分别完整读取同一包材料，每条线有独立结果与审核。当前七线依次执行，没有自动把上游分析当作下游已核验事实；估值、研报核查等资料不足时可以返回 `needs_data`。
3. **新闻或事件报告**：导入并选择 `industry-chain`。区分已披露事实、新闻线索及假设，输出传导条件与缺口；只有单篇新闻时不能假装已经取得公司敞口或订单原件。
4. **研究报告质量审核**：导入报告及可用证据，选择 `research-quality`；逐条查错另选 `report-factcheck`。资料不足时区分未核验与确认错误，不把规则检查当作专家质量分数。

命令示例中的文件、日期和 ID 均需替换为实际输入：

```text
python -m finresearch import report.pdf --published-at 2026-03-31T18:00:00+08:00
python -m finresearch case --document DOCUMENT_ID --task "分析盈利、现金流与风险" --cutoff 2026-04-01T09:00:00+08:00
python -m finresearch run CASE_ID --line financial-report
python -m finresearch run CASE_ID --all-lines
python -m finresearch workflow RUN_ID
```

`run`、网页研究按钮和连续批次会调用已配置模型，单条业务包含执行和独立审核。`workflow` 只读状态。

## 候选审核与专项流程

所有下面的命令默认操作同一工作台目录下的 `runtime/agent/`；指定 `finresearch --data-dir PATH` 时使用 `PATH/agent/`。直接 `python -m finagent` 也默认使用项目的 `runtime/agent/`，另一个资料库必须显式传入 `--state-dir`。

```text
python -m finresearch agent lines
python -m finresearch agent candidates AGENT_DOCUMENT_ID
python -m finresearch agent apply-review --review candidate_review.json
python -m finresearch agent ocr-jobs --status queued
python -m finresearch agent process-ocr OCR_JOB_ID --language chi_sim+eng --tesseract-cmd PATH_TO_TESSERACT
python -m finresearch agent fact-conflicts --entity-id ENTITY_ID --as-of 2026-04-01
python -m finresearch agent apply-resolution --resolution fact_resolution.json
python -m finresearch agent catalog-validate --catalog examples/pilot_catalog.example.json
python -m finresearch agent collect-sec-edgar --cik 320193 --output-dir public_data
python -m finresearch agent collect-cninfo-annual-reports
```

`AGENT_DOCUMENT_ID` 是工作流详情中登记的完整 SHA-256。审核 JSON 必须按 `CandidateReviewBundle` 契约填写候选、主体、值、单位、期间、范围和审核人等信息；操作记录写入本地 SQLite。上传一个自称已核验的 JSON 不会被工作台自动写为事实。专项 `finagent.financial.v1` JSON 是显式提供的结构化输入，未核验状态不代表专家确认；用于演示的 `examples/financial_report.json` 不能充当真实金标。

SEC 和巨潮命令默认 dry-run；只有显式 `--execute` 才会下载。SEC 执行还需提供可联系的真实 User-Agent。下载配额最大 2 GiB，公开采集不会自动产生金融事实。扫描 PDF 逐页提供模型图像；fork 的本地 OCR 执行器当前直接处理图片，扫描 PDF 尚需先导出页面图像。

公开仓库只包含人工生成的示例 JSON 与业务规范。样例 SQLite、金标库、公开原件、下载目录和作者试点现场不随代码发布；如在本机有独立取得的语料，可使用 `agent sample-tasks --corpus PATH`、`agent run-task --corpus PATH TASK_ID`、`agent evaluate --corpus PATH RUN_ID`。

## 失败、恢复和真实能力边界

- 工作台业务与审核仍是分开的模型请求，完整页面和引用校验继续生效。FinAgent 专项模型流程也使用完整文档准备与页面图像，超限直接停止，不截取前 12,000 字符。
- 工作台每次付费业务最多尝试一次。接口中断后保留原记录，使用既有 `recover`、`pipeline --resume` 或 `resume-audit` 流程；不会因为 DAG 重试而再次收费。专项 `agent resume` 不接受工作台工作流 ID。
- 正常中断和确证进程结束后的 `recover` 会同步关联 FinAgent 节点状态，保留已完成节点；续审核创建独立记录，保留原工作流关联来源。
- 缺失输出字段会保留 `partial` 和人工修订建议，规则审核不会反复审核同一个不完整 checkpoint 冒充修复。
- 原件、候选与已审核事实分开；页级定位有效也不等于该证据确实支持结论，仍需领域审核。
- 当前没有通用自主补料、自动跨线成果依赖、成熟专家评分或生产级多 worker 调度。完整七线尝试不等于每条线都适用或审核通过。

## 发布检查

```text
python scripts/check_publication.py --staged --sensitive-env
python scripts/check_publication.py --revision HEAD --sensitive-env
```

检查敏感文件、令牌格式、URL 认证、个人路径和实际敏感环境变量值，输出仅报告位置与规则名。实际密钥和端点只留在本机环境中；运行日志、提示词、原件、`.env`、本地配置、数据库和压缩包都由忽略规则及发布扫描阻止入库。扫描是发布校验，不能证明任意自由文本永远不含敏感数据。
