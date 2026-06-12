**线程总结**

1. 当前项目目录和目标  
项目目录：`D:\personal_news_assistant`  
目标：轻量版“每日 News 助手”。本地定时抓取 RSS，按兴趣关键词和 `feedback.yaml` 筛选新闻，验证链接和正文可读性，调用 OpenAI-compatible LLM 生成中文 Markdown 日报，并保存归档快照。另有生成后质量检查 MVP。

2. 已完成的修改  
已完成：
- Python/uv 项目初始化依赖。
- RSS 抓取：`feedparser`
- 正文抽取：`trafilatura`
- LLM 调用：`openai` SDK
- `.env` 读取：`python-dotenv`
- `config.yaml` 读取：`pyyaml`
- SQLite 去重：`news.db`
- 日报输出到 `outputs/`
- 日志写入 `logs/run.log`
- Windows 自动运行脚本 `run_news.bat`
- Windows 任务计划程序：任务名 `Daily News Assistant`，每天 `12:30` 运行。
- `archive/` 防覆盖：每次成功运行保存时间戳快照。
- 链接验证和正文可读性验证。
- `feedback.yaml` 反馈成长 v1。
- 生成后质量检查器 `quality_check.py` MVP。
- 质量检查报告输出到 `outputs/quality/latest_quality_report.md`。

3. 修改过的文件  
主要文件：
- `main.py`
- `config.yaml`
- `.env.example`
- `README.md`
- `run_news.bat`
- `AGENTS.md`
- `.gitignore`
- `feedback.yaml`
- `quality_check.py`
- `pyproject.toml`
- `uv.lock`

运行生成文件：
- `news.db`
- `logs/run.log`
- `logs/wrapper.log`
- `outputs/latest_daily_news.md`
- `outputs/daily_news_YYYY-MM-DD.md`
- `outputs/archive/daily_news_YYYY-MM-DD_HHMMSS.md`
- `outputs/quality/latest_quality_report.md`

4. 已验证的命令和结果  
已多次验证：
```powershell
uv run python -m py_compile main.py
```
结果：通过。

```powershell
uv run python main.py
```
结果：成功生成日报、latest、daily、archive 文件；日志写入正常。

```powershell
.\run_news.bat
```
结果：成功运行，`wrapper.log` 记录 wrapper 信息，`run.log` 由 Python logging 写入，避免了 Windows 文件占用冲突。

```powershell
uv run python -m py_compile quality_check.py
```
结果：通过。

```powershell
uv run python quality_check.py outputs/latest_daily_news.md
```
最近结果：
- Total news items: `2`
- PASS items: `2`
- WARN items: `0`
- FAIL items: `0`
- Overall WARN: `1`
- Overall FAIL: `0`
- 唯一整体 WARN：`low_value_with_optional_references`

任务计划程序已验证：
- 任务名：`Daily News Assistant`
- 程序：`D:\personal_news_assistant\run_news.bat`
- 起始于：`D:\personal_news_assistant`
- 每天：`12:30`
- `StartWhenAvailable=True`
- 手动运行结果：`LastTaskResult=0`

5. 当前未完成的问题  
仍存在：
- 部分 RSS 源 URL 失效或解析失败：
  - Anthropic News RSS 返回 404
  - NASA Earthdata RSS 返回 404
  - Papers with Code RSS 有解析 warning
- 日报 Markdown 格式仍由 LLM 生成，不是完全结构化输出，质量检查只能宽松解析。
- `quality_check.py` 目前只做确定性检查，不调用 LLM，不用 Playwright/Stagehand。
- 低价值日报有时会同时出现“今日无高价值新闻”和“可选参考”，目前质量检查会整体 WARN。
- `news.db` 去重后，同一天重复运行可能没有新候选，这是预期行为，但会导致 latest 变成低价值报告；daily 文件当前设计为低价值报告不覆盖当天最终版。

6. 下一步建议  
建议下一步：
- 给 `quality_check.py` 增加退出码策略，例如有 FAIL 时 exit 1，方便任务计划或 CI 判断。
- 在 `run_news.bat` 后追加运行 `quality_check.py`，但先确认是否希望自动质量检查也写 wrapper 日志。
- 修复或替换失效 RSS 源：
  - Anthropic RSS
  - NASA Earthdata RSS
  - Papers with Code RSS
- 让 `main.py` 输出更稳定的 Markdown 结构，或额外保存 JSON 元数据，方便质量检查精确解析。
- 为 `quality_check.py` 增加 `outputs/quality/archive/quality_report_YYYY-MM-DD_HHMMSS.md`，避免质量报告覆盖。
- 根据实际日报继续更新 `feedback.yaml`。

7. 重要约束  
不要动或谨慎动：
- 不要删除 `news.db`，除非用户明确允许。
- 不要删除 `outputs/`、`outputs/archive/`、`logs/`。
- 不要把真实 API Key 写进代码、README 或日志。
- 不要修改 Windows 任务计划程序，除非用户明确要求。
- 不要引入 Docker。
- 不要创建前端。
- 不要引入重型框架、向量库、多 Agent、模型微调。
- 不要修改邮件发送逻辑，目前邮件功能还没做。
- 修改 `main.py` 后必须至少运行：
  ```powershell
  uv run python -m py_compile main.py
  ```
- 修改 `quality_check.py` 后必须至少运行：
  ```powershell
  uv run python -m py_compile quality_check.py
  ```
- 新增配置项必须同步更新 `README.md`。
- RSS 抓取、链接验证、正文验证、LLM 调用逻辑已经能跑，除非明确要求，不要大改。