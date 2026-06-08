# Relay Model Audit

这是一个本地运行的 AI 中转站验货工具，用来对 OpenAI-compatible 或 Anthropic-compatible API 做快速风险筛查。它不会证明某个中转站和官方模型完全等价，但可以用一组可复现的探针，发现明显的模型替换、协议伪造、能力缩水、系统提示注入、流式假支持、错误信息泄露等问题。

适合在没有官方 key 的情况下，对多个候选中转站做横向比较。测试结果只应作为工程验货参考，不应当作为模型真实性的唯一结论。

## 功能

- 协议连通性与响应结构检查
- 响应签名检查：`id`、`model`、`usage`、`finish_reason` / `stop_reason`
- 内容 canary：随机 nonce 回显，检测模板化、改写或截断
- System 指令覆盖：检测是否存在上游或中转注入的更高优先级提示
- 能力指纹：多约束 JSON、代码推理、逻辑题、上下文针
- 性能稳定性：重复采样、失败率、延迟波动、答案一致性
- SSE 流式完整性：`Content-Type`、chunk、TTFB、结束信号、聚合文本
- 错误信息泄露：畸形请求是否暴露上游 URL、key、堆栈或内部路径

## 安全约定

不要把真实 API key 写进配置文件。请使用环境变量传入 key：

```powershell
$env:RELAY_A_KEY="replace_with_temporary_key"
$env:RELAY_B_KEY="replace_with_temporary_key"
```

建议只使用临时 key 做测试，测完后在中转站后台删除或重置。本项目的 `.gitignore` 已默认排除 `sites.json`、`.env` 和原始响应日志。

## 快速开始

进入项目目录：

```powershell
cd "$env:USERPROFILE\OneDrive\Desktop\模型中转站验货"
```

复制配置模板：

```powershell
Copy-Item .\sites.example.json .\sites.json
```

编辑 `sites.json`，填入候选站点：

```json
{
  "sites": [
    {
      "name": "候选中转站A",
      "base_url": "https://example-a.com/v1",
      "model": "target-model-name",
      "api_key_env": "RELAY_A_KEY",
      "timeout": 90
    }
  ]
}
```

运行轻量验货：

```powershell
python .\ztest_style_light_audit.py --config .\sites.json
```

如果你有官方 API key，也可以运行官方和中转对照检测：

```powershell
$env:OFFICIAL_API_KEY="replace_with_official_key"
$env:OFFICIAL_BASE_URL="https://api.openai.com/v1"
$env:OFFICIAL_MODEL="official-model-name"

$env:RELAY_API_KEY="replace_with_relay_key"
$env:RELAY_BASE_URL="https://relay.example.com/v1"
$env:RELAY_MODEL="relay-model-name"

python .\run_model_audit.py
```

## 输出

结果会写入 `results/`：

- `*_report_*.md`：人读报告
- `*_summary_*.csv`：表格汇总
- `*_raw*.json` / `*_raw*.jsonl`：原始响应，默认不建议上传

## 分数解读

- `80+`：未发现明显异常，但不代表绝对真实
- `60-80`：基本可用，但存在短板或可疑点
- `40-60`：可疑，需要人工查看原始响应
- `20-40`：强可疑
- `0-20`：基本不可用或严重异常

## 关于 ztest.ai 思路

本项目参考了 ztest.ai 公开页面展示的探针思路，但不调用 ztest.ai，也不绕过 Turnstile。公开可见的思路包括协议探针、身份一致性、隐式身份、内容 canary、结构化输出、性能稳定性、响应签名、系统指令覆盖、错误信息泄露、流完整性等。

这里实现的是本地简化版，适合个人快速筛选候选中转站。

## 注意

- 测试可能触发中转站计费，请先确认价格和额度。
- 结论只是风险提示，不等于法律、财务或安全背书。
- 上传或分享项目前，请确认没有提交 `sites.json`、`.env`、原始响应 JSON 或任何真实 key。
