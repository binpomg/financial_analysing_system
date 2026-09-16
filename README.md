# financial_analysing_system · 金融研究工作台

以A股公司与产业研究为起点的本地研究系统。LLM完整阅读原件，各业务线以独立Skill执行，新上下文重新审核；程序负责原件保存、页面准备、精确计算、证据校验和版本记录。

已提供七条业务线：数据结构化提取、财务报告分析、产业链影响分析、估值建模、研报事实核查、投资备忘录、研究质量评估。可手工导入材料，也可按需采集公开公告，逐份开展七线研究和反馈积累。候选Skill需要独立验证与历史回归，**默认不自动晋升稳定版本**。

## 安装与离线检查

支持以 **Python 3.12 + 完整源码checkout** 运行。`resources/`、`web/`、`config.json` 必须与 `finresearch/` 一起保留；当前不支持通过wheel安装或只复制Python包部署。

Windows PowerShell：

```powershell
git clone https://github.com/binpomg/financial_analysing_system.git
Set-Location financial_analysing_system
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m unittest discover -s test -v
.\.venv\Scripts\python.exe -m finresearch doctor
.\.venv\Scripts\python.exe -m finresearch serve --port 8765
```

Linux Bash：

```bash
git clone https://github.com/binpomg/financial_analysing_system.git
cd financial_analysing_system
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m unittest discover -s test -v
python -m finresearch doctor
python -m finresearch serve --port 8765
```

也可在完整clone后运行安装脚本：Windows用 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1`，Linux用 `bash scripts/setup.sh`。脚本创建虚拟环境并安装锁定依赖；已有环境只检查版本，不自动升级或删除。

安装依赖需要网络；测试和不带 `--live` 的 `doctor` 不调用模型，无需密钥。本次发布在Windows、Python 3.12.14下通过 **287项离线测试**，并验证全新环境安装、空资料库和工作台HTTP启动；测试时阻断外部网络连接，未调用真实模型。GitHub Actions在Windows与Ubuntu上重复验收，最新结果见仓库Actions。测试范围和历史实测限制见 [验证说明](docs/validation.md)。

打开 [本机工作台](http://127.0.0.1:8765)。未配置接口时，可以查看界面和管理本地材料，模型研究不会启动。前台服务在终端中运行，按 `Ctrl+C` 结束；需要模型调用时，先停止服务，按下节配置后在同一终端重新启动。

## 接口与使用

仓库不包含API密钥、模型服务地址、个人配置、运行日志或历史原件。使用者需在本机设置 `FIN_API_BASE_URL` 与 `FIN_API_KEY`，然后从同一环境启动CLI或工作台；`.env` 文件不会自动加载。隐藏输入及跨平台步骤见 [使用说明](docs/usage.md)。

| 职责 | 配置模型ID | 推理强度 |
|---|---|---|
| 公开目录整理 | `gpt-5.6-luna` | `xhigh` |
| 业务、独立审核、反馈核实、候选生成 | `gpt-6-astra` | `xhigh` |

接口须支持配置的模型ID、流式响应、结构化输出、页面图像及计算工具。默认协议为 `responses`，可显式改为 `chat_completions`；系统不会自动更换模型或降低推理强度。

真实调用由使用者显式发起：在工作台启动研究，或运行 `run`、`pipeline`、`campaign-run` 等命令。`doctor --live` 也会真实调用模型。同一材料包含多条业务、审核及可能的计算／核实请求，不等于一次模型调用。

- [使用说明](docs/usage.md)：本地配置、导入、采集、单份流程、有限批次与恢复。
- [架构说明](docs/architecture.md)：业务隔离、证据、数据用途及Skill改进边界。
- [验证说明](docs/validation.md)：程序测试与历史10份实测摘要。
- [评价与版本管理](docs/evaluation.md)：对照、回归、分区及晋升政策。

历史10份材料共完成70次业务线尝试，48次达到完整审核终态：30次有效通过、11次需修改、7次证据不足；另19次失败、3次中断，积累13条核实反馈，新增候选与晋升均为0。这是有限样本的历史摘要，不能证明七线业务成熟、可复现相同模型输出或具有投资收益。
