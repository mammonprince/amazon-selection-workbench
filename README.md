# 亚马逊 AI 选品工作台 · v0.4.6

Amazon US 单小类目的本地选品工具。上传卖家精灵畅销榜和可选新品榜，保留来源，按 ASIN 复核历史明细。四个模块为类目评分、新品候选、改良品候选和 AI 调研摘要。

## Windows 启动

需要 Python 3.10 或以上。解压后进入含 `app.py` 的项目目录，在 PowerShell 运行：

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

第一行安装依赖，第二行启动网页。浏览器打开 http://localhost:8501。停在运行窗口时按 Ctrl+C 可以退出。

## 试用

- 先用内置样例看候选，验证父体月销 200 的记录会入选，199 不会入选。
- 上传畅销榜普通导出，可再上传同小类目的新品榜及两榜各自的历史明细。普通导出选择含 ASIN 的商品页（一般为 US）。
- 标准卖家精灵普通导出会把「月销量」自动映射到父体月销量。其他格式可手动映射。
- 在候选页核对来源、季节性、价格异动和搜索词。搜索词在正文，鼠标移到词框右侧可复制。真实 ASIN 可点击，模拟编号以 DEMO 开头。
- 查看摘要页的本地报告或下载 Markdown。更完整规则见 [PRD.md](PRD.md)。

## 可选模型

在左侧选择供应商，在「API 密钥」密码框粘贴对应的 Key 并按 Enter。默认模型已经填写，也可以在「模型设置」修改。网页输入只用于当前会话。点击生成报告、AI 分组或排除预览时才发起调用。

| 供应商 | 默认模型 | 可选环境变量 |
| --- | --- | --- |
| 百炼 / Qwen | `qwen3.7-flash` | `DASHSCOPE_API_KEY`、`DASHSCOPE_MODEL` |
| OpenAI / ChatGPT | `gpt-5-mini` | `OPENAI_API_KEY`、`OPENAI_MODEL` |
| Claude | `claude-haiku-4-5` | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` |

如需使用百炼其他地域或业务空间的端点，可以在启动前设置 `DASHSCOPE_BASE_URL` 为百炼控制台提供的 OpenAI 兼容 Base URL。实际供应商调用待验收，样例的本地计算和模板报告可直接试用。

## 参考与许可

七项评分算法及风险词模块来自 [Peter Wong 的选品项目](https://github.com/Peterwong666/Ai_Agent_for_e-commerce/tree/e930c97df4bfffb6aa38dab26e11a5399d539e26)，原 MIT 许可保留在 `peter/LICENSE`。12 节报告和改良品筛法以同一项目为参考。
