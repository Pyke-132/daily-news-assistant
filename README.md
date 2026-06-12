# Personal News Assistant

一个轻量版“每日 News 助手”：每天抓取固定 RSS 新闻源，根据你的兴趣关键词筛选新闻，调用 OpenAI-compatible LLM 生成中文 Markdown 摘要，并保留每条新闻的原文链接。

第一版只面向本地运行：没有前端、没有 Docker、没有复杂数据库系统，只使用 SQLite 记录已处理新闻。

## 功能

- 从 `config.yaml` 读取 RSS 源、关键词和默认设置。
- 用 `feedparser` 抓取 RSS。
- 用 `trafilatura` 抽取网页正文。
- 在候选进入 LLM 前验证原文链接状态和正文可读性，避免坏链或弱正文被过度总结。
- 对标题、RSS 摘要、正文进行关键词打分。
- 用 `openai` SDK 调用 OpenAI-compatible API 生成中文 Markdown 日报。
- 如果新闻涉及可能不熟悉的概念，日报会先补充 3-5 句话前置背景。
- 用 `sqlite3` 保存已处理新闻，避免重复处理。
- 输出日报到 `outputs/`，同时保存最新日报、当天最终版和归档快照。
- 写运行日志到 `logs/run.log`。

## 评分说明

程序内部会计算一个 `keyword_score`，用于候选新闻排序和传给 LLM 参考。这个分数可能很大，例如 169 或 491，不等于日报里的最终“相关性评分”。

日报中展示的“相关性评分”由 LLM 根据用户背景、新闻内容和学习价值重新判断，必须是 1-10 分。

## 安装依赖

本项目使用 uv 管理依赖。

```powershell
uv sync
```

如果你需要重新添加依赖，可以运行：

```powershell
uv add feedparser trafilatura openai python-dotenv pyyaml beautifulsoup4 requests
```

## 配置 .env

复制示例文件：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

说明：

- `OPENAI_API_KEY`：你的真实 API Key。
- `OPENAI_BASE_URL`：OpenAI 或其他 OpenAI-compatible 服务的接口地址。
- `OPENAI_MODEL`：要使用的模型名称，需要和你的服务商支持的模型一致。

不要把真实 API Key 写进代码、README 或提交到 Git。

## 运行

```powershell
uv run python main.py
```

运行结束后，终端会打印输出文件路径。

## 查看 outputs

每次成功生成日报时，程序会同时保存三份文件：

```text
outputs/latest_daily_news.md
```

`latest_daily_news.md` 是最新日报，每次运行成功都会覆盖为最新内容。

```text
outputs/daily_news_YYYY-MM-DD.md
```

`daily_news_日期.md` 是当天最终版，同一天多次运行时会被最新结果覆盖。

```text
outputs/archive/daily_news_YYYY-MM-DD_HHMMSS.md
```

`archive/` 保存每次运行的快照，文件名包含日期和时间，防止同一天多次运行时丢失早先结果。

例如一次运行可能生成：

```text
outputs/latest_daily_news.md
outputs/daily_news_2026-06-09.md
outputs/archive/daily_news_2026-06-09_083000.md
```

如果当天没有命中兴趣关键词且未处理过的新闻，也会生成一份简短报告。

## 链接和正文质量

程序会在候选新闻进入 LLM 前检查原文链接和正文可读性：

- `url_status` 记录原文链接 HTTP 状态码，`404/410/500/502/503/504` 会被标记为 `invalid_link`。
- `content_length` 记录正文抽取后的字符数。
- 正文少于 500 字符会被标记为 `weak_content`。
- `content_available=false` 的候选不会让 LLM 生成详细内容总结。
- 如果链接失效，日报只提示“原链接不可用，建议搜索”，不会自动猜测正确链接。
- 如果所有候选都是坏链或弱正文，日报会生成“今日无高质量可读新闻”，不会硬凑推荐。

## 反馈成长

`feedback.yaml` 用来记录你的轻量偏好，让日报逐步更贴近你的方向。程序启动时会自动读取它；如果文件不存在，会使用空反馈继续运行，不会报错。

可以在 `likes.topics` 和 `likes.sources` 中添加你希望提高优先级的主题和来源，例如 `RAG`、`Hugging Face Blog`。这些命中标题、摘要、正文或来源时，会提高候选排序分。

可以在 `dislikes.topics` 和 `dislikes.sources` 中添加你希望降低优先级的主题和来源，例如 `crypto`、`weakly related voice translation`。这些命中时会降低候选排序分，弱相关内容更不容易进入最终日报。

`learning_preferences` 会传给 LLM，用来约束总结风格，例如先补前置背景、判断是否真有用、弱相关时直接说可跳过。

`daily_feedback` 用来记录每天喜欢、不喜欢、发现的问题和备注。最近 7 条 daily feedback 会传入 LLM prompt，帮助后续日报调整推荐和表达。

修改 `feedback.yaml` 后，下次运行 `uv run python main.py` 会自动生效。它只是轻量规则和提示词上下文，不是向量库、模型微调或长期记忆数据库。

## 每日自动化：Windows 任务计划程序

可以用 `run_news.bat` 每天自动运行。当前脚本会先 `cd /d` 到项目根目录，再执行 `uv run python main.py`，并使用 `>> logs\run.log 2>&1` 追加日志，不会覆盖旧日志。

1. 打开 Windows “任务计划程序”。
2. 点击“创建基本任务”。
3. 名称可以填写 `Personal News Assistant Daily Run`。
4. 触发器选择“每天”，时间设置为 `12:30`。
5. 操作选择“启动程序”。
6. “程序或脚本”填写 `run_news.bat` 的绝对路径：

```text
D:\personal_news_assistant\run_news.bat
```

7. “起始于”填写项目根目录：

```text
D:\personal_news_assistant
```

8. 完成创建后，在任务计划程序列表中找到该任务，右键选择“运行”，可以手动触发一次测试。
9. 测试后查看日报输出：

```text
D:\personal_news_assistant\outputs\
```

10. 查看运行日志：

```text
D:\personal_news_assistant\logs\run.log
```

## 常见报错

### 401 Unauthorized

通常是 API Key 不正确、过期，或 `.env` 没有正确加载。

检查：

- `.env` 是否存在。
- `OPENAI_API_KEY` 是否是真实 key。
- key 是否属于你配置的 `OPENAI_BASE_URL` 对应服务商。

### 404 model not found

通常是 `OPENAI_MODEL` 写错，或当前 API 服务商不支持该模型。

检查：

- 模型名称是否拼写正确。
- 模型是否已在你的账号或服务商中开通。
- `OPENAI_BASE_URL` 是否指向正确服务商。

### RSS 抓取失败

单个 RSS 源失败不会导致整个程序崩溃，失败细节会写入 `logs/run.log`。

可能原因：

- RSS URL 临时不可用。
- 网站屏蔽请求。
- 网络代理或 DNS 配置异常。

可以在 `config.yaml` 中临时移除不可用 RSS 源，或稍后重试。

### 网络连接失败

可能发生在 RSS 抓取、网页正文抽取或 LLM 调用阶段。

检查：

- 当前网络是否能访问 RSS 网站。
- 是否需要代理。
- `OPENAI_BASE_URL` 是否可访问。
- 服务商是否发生故障或限流。
