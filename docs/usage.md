# 部署与使用

先按 [README](../README.md) 完整clone并用Python 3.12安装 `requirements.lock.txt`。以下 `python` 均指项目虚拟环境：Windows可将它替换为 `.\.venv\Scripts\python.exe`，Linux先执行 `source .venv/bin/activate`。所有命令从仓库根目录运行。

## 在本机配置接口

默认配置仅从 `FIN_API_BASE_URL`、`FIN_API_KEY` 读取地址和认证。不要将实际值写进命令文本、仓库配置、讨论区或截图；下面的隐藏输入不会把值写进shell历史。`.env` 不会自动加载，单独创建该文件不会完成配置。

Windows PowerShell，在准备启动研究的同一终端输入：

```powershell
$apiBaseInput = Read-Host '输入API基础地址' -AsSecureString
$env:FIN_API_BASE_URL = [System.Net.NetworkCredential]::new('', $apiBaseInput).Password
$apiKeyInput = Read-Host '输入API密钥' -AsSecureString
$env:FIN_API_KEY = [System.Net.NetworkCredential]::new('', $apiKeyInput).Password
Remove-Variable apiBaseInput, apiKeyInput
.\.venv\Scripts\python.exe -m finresearch serve --port 8765
```

Linux Bash：

```bash
read -r -s -p '输入API基础地址: ' FIN_API_BASE_URL
printf '\n'
read -r -s -p '输入API密钥: ' FIN_API_KEY
printf '\n'
export FIN_API_BASE_URL FIN_API_KEY
python -m finresearch serve --port 8765
```

基础地址必须是无认证信息、查询参数或片段的HTTPS地址，路径由服务提供方说明；不要填成某一次请求的完整端点。系统自行追加协议端点。上述变量只为当前会话及其子进程配置；新终端或服务管理器启动的进程需要单独配置。已启动的工作台不会获得之后才设置的变量，应停止后从已配置的终端重启。

非敏感覆盖项可写到本机 `config.local.json`，例如：

```json
{"api":{"protocol":"chat_completions"}}
```

默认协议为 `responses`。使用的服务须支持 `gpt-5.6-luna`、`gpt-6-astra`、`xhigh`、JSON Schema、页面图像、流式终态及业务计算工具；不支持时记录失败，不自动换模。密钥及实际接口地址不应写入此覆盖文件。

配置接口后，本机 `doctor` 输出、`/api/state`、`/api/run`、导出JSON和 `runtime/` 中的trace可能包含服务主机名、资料或本地路径。这些内容仅供本机诊断，分享前必须脱敏；不要将截图、日志或导出结果直接提交到公开仓库。

## 离线检查与真实调用

```text
python -m unittest discover -s test -v
python -m finresearch doctor
python -m finresearch status
```

这些命令不调用模型。未配置认证时，`doctor` 仍可检查Python、依赖与配置，并报告认证不可用。`serve` 只启动工作台，点击研究按钮才会请求模型。

下列命令会发起真实模型调用，需自行承担对应接口费用：

| 命令 | 调用范围 |
|---|---|
| `doctor --live` | 对目录模型和业务模型作接口检查 |
| `discover`、`collect` | Luna整理来源目录；采集本身也会访问公开网站 |
| `run`、`resume-audit` | 指定业务与审核，或仅续接审核 |
| `pipeline`、`campaign-run` | 采集、七线独立研究、审核、核实及可能的候选生成 |
| `verify-feedback`、`propose`、`experiment` | 问题核实、候选生成或新旧对照与回归 |

每份原件可能产生多次独立请求与计算轮次，耗时及费用随材料和服务而变。

## 手工材料研究

先导入原件，使用真实公开时间与带时区的时间戳。下面文件名、日期和ID是格式示例，应替换为自己的资料；系统不提供这些原件。

```text
python -m finresearch import announcement.pdf --published-at '2026-01-15T18:00:00+08:00' --group 'company-event-001'
python -m finresearch case --document '<文档ID>' --task '完整提取公告事件，保留主体、金额、期间、阶段与原文证据' --cutoff '2026-01-16T09:00:00+08:00'
python -m finresearch run '<案例ID>' --line extraction
```

前两步为本地准备，最后一步真实调用模型。多个原件通过重复 `--document` 组成材料包；必需附件可用 `--attachment` 登记。关联事件、修订及转载应使用同一资料组。默认案例用于 `development`，不能将已用于改进的资料改标签后当作新验证集。

`run '<案例ID>' --all-lines` 执行全部七线，每线独立获得完整材料。成果含JSON、CSV、Markdown与审核状态，可在工作台查看和导出。

## 自动采集与有限连续任务

单份入口：

```text
python -m finresearch pipeline
```

也可点击工作台“启动1份材料的小批次”。每批最多1份可用原件，默认从近七日“中标”“经营数据”目录采集；已取得且尚未使用的原件优先复用。没有合格新材料时，保留原因并结束。

连续处理需先明确数量，再显式运行，例如：

```text
python -m finresearch campaign-create --documents 10
python -m finresearch campaign-run '<任务ID>'
```

创建命令不请求模型；运行命令才开始付费流程。数量范围1—100，逐份处理，达到目标后结束。连续任务冻结90日目录窗口及方法、模型和运行环境；每源最多10页、每页30条。来源不足或连续无法获得完整审核等情况会提前停下，不把未完成材料算作成功。目标数量指进入研究的原件，不是网络下载尝试总数。

工作台可查看逐线进度并请求在当前材料结束后暂停。命令行状态检查为 `campaign-status '<任务ID>'`。前台运行会占用该终端；上述方式不创建系统定时任务或开机自启。要在另一终端查看，需指向同一资料库。

## 中断与结果判断

关机、接口失败和流式中断会保留原运行。`python -m finresearch recover` 只将已确认死亡进程的记录标为中断，不发起模型请求。之后可显式执行 `pipeline --resume '<批次ID>'` 或 `campaign-run '<任务ID>'`，只继续未发起步骤；运行环境或方法已变化时不能直接续接。暂停控制文件仍存在时，连续任务会保持暂停，应先核对原暂停意图，不用删除失败记录来恢复。

已完成业务但审核失败时，`resume-audit '<运行ID>'` 是单独授权的真实审核调用，保留原失败；这种交付恢复不能伪装成同环境的新验证证据。

| 状态 | 含义 |
|---|---|
| `audited` | 完成独立审核，仍须查看裁决 |
| `audit_passed` | 当前任务审核通过，不等于完整投资研究成熟 |
| `needs_data` | 当前材料不足，需按结果补齐输入 |
| `not_applicable` | 全文阅读后判断当前业务不适用 |
| `requires_review` | 存在修改要求或证据不足 |
| `failed`、`interrupted`、`blocked` | 失败、中断或不能交付，不能算作通过 |
| `completed_with_issues` | 批次步骤已结束，但保留以上问题 |

反馈核实与候选生成不会自动替换稳定Skill。新旧对照和回归必须预登记独立材料，正式晋升默认关闭，详见 [评价说明](evaluation.md)。本地资料库应自行备份，不能将它作为公开代码一并提交。
