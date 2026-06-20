# Relay Model Audit · 中转 API 双向验货

本地运行的 AI 中转站验货工具。给一个 **OpenAI 格式**（`/chat/completions`）或 **Anthropic 格式**（`/v1/messages`）的中转 API，用一组可复现的探针快速做风险筛查；更进一步，把**两个中转端摆在一起横向比对打分**，在没有官方 key 的情况下也能判断"哪个更靠谱、是否疑似缩水替换"。

它不证明某个中转站和官方模型完全等价，但能发现明显的**模型替换、协议伪造、能力缩水、系统提示注入、流式假支持、错误信息泄露**等问题。

> 结果仅作工程验货参考，不构成模型真实性、法律或财务背书。

---

## 亮点

- **双中转横评**：A、B 两端各跑全套探针并排打分，再做 A↔B 一致性交叉校验（同一批确定性题答案是否分叉、自报模型名是否一致、谁更稳），自动给出"推荐谁 / 关键差距 / 是否疑似缩水替换"的结论。
- **双协议**：OpenAI 与 Anthropic 原生协议都支持，可混合比对（如 OpenAI 端 vs Anthropic 端）。
- **模型名一键获取**：填好 Base URL + Key 点一下，自动拉取该站 `/models` 列表，下拉选择即可——不同中转站模型名各异，不用再手敲。
- **本地 WebUI**：浏览器里填参数、点一下、实时看进度和结果。仅绑定 `127.0.0.1`，外部访问不到。
- **零第三方依赖**：纯 Python 标准库，`python` 一跑就能用。
- **key 安全**：key 只在本机内存中使用、跑完即弃，不写磁盘、不上传。
- **抗 Cloudflare 拦截**：出站请求带浏览器 UA，绕开按默认 `Python-urllib` UA 拦截的中转站（Cloudflare 1010 `browser_signature_banned`）。

## 探针

| 探针 | 检查点 |
|---|---|
| 协议连通性与响应签名 (D1/D2/D17) | `id`、`model`、`usage`、`finish_reason`/`stop_reason` 是否齐全合规 |
| 内容 canary (D5) | 随机 nonce 回显，检测模板化、改写或截断 |
| System 指令锁定 (S3) | 是否存在上游或中转注入的更高优先级提示 |
| 能力指纹 (D16) | 多约束 JSON、代码推理、逻辑陷阱 |
| 性能稳定性 (D8/D9) | 重复采样、失败率、延迟波动、答案一致性 |
| SSE 流式完整性 (S5) | `Content-Type`、chunk、TTFB、结束信号、聚合文本 |
| 错误信息泄露 (S4) | 畸形请求是否暴露上游 URL、key、堆栈或内部路径 |

---

## 快速开始（WebUI，推荐）

需要 Python 3.10+，**无需安装任何依赖**。

```bash
git clone https://github.com/hhzz-svg/relay-model-audit.git
cd relay-model-audit
python webui.py
```

浏览器会自动打开 `http://127.0.0.1:8731/`。操作三步：

1. 左右两栏分别填中转 A / B 的**协议、Base URL（含 `/v1`）、API Key**。
2. 点模型名旁的「**获取**」自动拉取该站可用模型，从下拉里选一个（也可手填）。
3. 点「**开始比对**」，实时查看逐探针打分、分项对比与一致性校验结论。

```bash
python webui.py --port 9000     # 换端口
python webui.py --no-browser    # 不自动开浏览器
```

> ⚠️ **必须通过 `python webui.py` 启动后再访问页面。** 直接双击打开 `web/index.html`、或用编辑器的预览面板看，页面来源是 `file://` / 沙箱环境，`/api/*` 接口不存在，会报 `Failed to parse URL from /api/run`。页面检测到这种情况会给出红色提示。

## 命令行用法

复制配置模板并填入候选站点（注意 `protocol` 字段可填 `openai` 或 `anthropic`，key 用环境变量名而非明文）：

```bash
cp sites.example.json sites.json
```

```json
{
  "sites": [
    {
      "name": "候选中转站A",
      "protocol": "openai",
      "base_url": "https://example-a.com/v1",
      "model": "target-model-name",
      "api_key_env": "RELAY_A_KEY",
      "timeout": 90
    }
  ]
}
```

```bash
# 对配置里每个站点各自独立打分
python ztest_style_light_audit.py --config sites.json

# 取前两个站点做双中转横评
python ztest_style_light_audit.py --config sites.json --compare

# 调整稳定性探针采样次数（默认 5）
python ztest_style_light_audit.py --config sites.json --compare --samples 8
```

如果你有官方 API key，仍可用官方对照检测（OpenAI 格式）：

```bash
export OFFICIAL_API_KEY="..."  OFFICIAL_BASE_URL="https://api.openai.com/v1"  OFFICIAL_MODEL="..."
export RELAY_API_KEY="..."     RELAY_BASE_URL="https://relay.example.com/v1"  RELAY_MODEL="..."
python run_model_audit.py
```

## 离线自测（不花钱、不需真 key）

内置一个本地假中转服务，把双协议解析、打分、SSE、横评、模型列表拉取全链路跑通：

```bash
python selftest.py
```

看到 `ALL PASS` 即表示核心逻辑正常。

---

## 分数解读

| 区间 | 含义 |
|---|---|
| `80+` | 未发现明显异常（不代表绝对真实） |
| `60–80` | 基本可用，但存在短板或可疑点 |
| `40–60` | 可疑，建议人工查看原始响应 |
| `20–40` | 强可疑 |
| `0–20` | 基本不可用或严重异常 |

协议连通性 / 系统指令锁定 / 错误信息泄露存在硬伤时会触发**封顶**，综合分不会超过对应上限。

## 安全约定

- **不要把真实 API key 写进配置文件**，用环境变量传入；建议只用临时 key，测完在中转站后台删除或重置。
- `.gitignore` 已默认排除 `sites.json`、`.env`、`results/`。
- WebUI 在网页里填的 key 只在内存中使用、跑完即弃，不落盘、不上传。
- 测试会**真实触发中转站计费**，请先确认价格和额度。

## 关于 ztest.ai 思路

本项目参考了 ztest.ai 公开页面展示的探针思路，但**不调用 ztest.ai，也不绕过任何验证**。这里是本地简化版，不做榜单，主打两个中转端的对比与打分。

更多设计与排错见 [`docs/架构与使用.md`](docs/架构与使用.md)。
