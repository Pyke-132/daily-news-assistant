from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
import trafilatura
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
FEEDBACK_PATH = ROOT_DIR / "feedback.yaml"
LOG_DIR = ROOT_DIR / "logs"
LOG_PATH = LOG_DIR / "run.log"


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    summary: str
    body: str
    score: int
    matched_keywords: list[str]
    feedback_score: int
    adjusted_score: int
    feedback_reasons: list[str]
    url_status: int | str
    content_length: int
    content_available: bool
    invalid_link: bool
    weak_content: bool
    suggested_search_query: str
    fallback_url: str = ""
    published: str = ""


INVALID_STATUS_CODES = {404, 410, 500, 502, 503, 504}
MIN_CONTENT_LENGTH = 500
MIN_ADJUSTED_SCORE = 1
LIKE_TOPIC_WEIGHT = 60
LIKE_SOURCE_WEIGHT = 20
DISLIKE_TOPIC_WEIGHT = -200
DISLIKE_SOURCE_WEIGHT = -50


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    required_keys = ["rss_sources", "keywords"]
    missing = [key for key in required_keys if key not in config]
    if missing:
        raise ValueError(f"config.yaml is missing required keys: {', '.join(missing)}")

    return config


def empty_feedback() -> dict[str, Any]:
    return {
        "likes": {"topics": [], "sources": []},
        "dislikes": {"topics": [], "sources": []},
        "learning_preferences": [],
        "daily_feedback": [],
    }


def load_feedback() -> dict[str, Any]:
    feedback = empty_feedback()
    if not FEEDBACK_PATH.exists():
        logging.info("feedback.yaml not found. Using empty feedback.")
        return feedback

    with FEEDBACK_PATH.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}

    for section in ("likes", "dislikes"):
        section_data = loaded.get(section) or {}
        feedback[section]["topics"] = list(section_data.get("topics") or [])
        feedback[section]["sources"] = list(section_data.get("sources") or [])
    feedback["learning_preferences"] = list(loaded.get("learning_preferences") or [])
    feedback["daily_feedback"] = list(loaded.get("daily_feedback") or [])

    logging.info(
        "Loaded feedback.yaml: like_topics=%s like_sources=%s dislike_topics=%s dislike_sources=%s "
        "learning_preferences=%s daily_feedback=%s",
        len(feedback["likes"]["topics"]),
        len(feedback["likes"]["sources"]),
        len(feedback["dislikes"]["topics"]),
        len(feedback["dislikes"]["sources"]),
        len(feedback["learning_preferences"]),
        len(feedback["daily_feedback"]),
    )
    return feedback


def load_llm_settings() -> tuple[str, str | None, str]:
    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    model = os.getenv("OPENAI_MODEL", "").strip()

    if not api_key or api_key == "your_api_key_here":
        raise RuntimeError("OPENAI_API_KEY is not configured. Copy .env.example to .env and fill in a real key.")
    if not model:
        raise RuntimeError("OPENAI_MODEL is not configured in .env.")

    return api_key, base_url, model


def init_db(db_path: str) -> sqlite3.Connection:
    resolved_path = ROOT_DIR / db_path
    conn = sqlite3.connect(resolved_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_news (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            score INTEGER NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def make_news_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_processed(conn: sqlite3.Connection, url: str) -> bool:
    news_id = make_news_id(url)
    row = conn.execute("SELECT 1 FROM processed_news WHERE id = ?", (news_id,)).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, items: list[NewsItem]) -> None:
    processed_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (make_news_id(item.url), item.url, item.title, item.source, item.score, processed_at)
        for item in items
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO processed_news (id, url, title, source, score, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fetch_rss_entries(source: dict[str, str], max_items: int, session: requests.Session) -> list[Any]:
    name = source.get("name", "Unnamed source")
    url = source.get("url", "")
    if not url:
        logging.warning("RSS source %s has no url, skipped.", name)
        return []

    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False):
            logging.warning("RSS source %s parsed with warning: %s", name, getattr(feed, "bozo_exception", "unknown"))
        return list(feed.entries[:max_items])
    except Exception as exc:
        logging.exception("Failed to fetch RSS source %s (%s): %s", name, url, exc)
        return []


def is_success_status(status: int | str) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def fetch_article_content(url: str, session: requests.Session) -> tuple[int | str, str]:
    try:
        response = session.get(url, timeout=8)
        status = response.status_code
        if not is_success_status(status):
            return status, ""

        extracted = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted:
            return status, normalize_space(extracted)
        return status, normalize_space(strip_html(response.text))
    except Exception as exc:
        logging.warning("Failed to extract article text from %s: %s", url, exc)
        return "request_failed", ""


def is_invalid_link(status: int | str) -> bool:
    if status == "request_failed":
        return True
    return isinstance(status, int) and status in INVALID_STATUS_CODES


def get_hacker_news_fallback(entry: Any) -> str:
    comments = str(entry.get("comments", "") or "").strip()
    if comments:
        return comments

    for link in entry.get("links", []) or []:
        href = str(link.get("href", "") or "").strip()
        if "news.ycombinator.com/item?id=" in href:
            return href

    entry_id = str(entry.get("id", "") or "").strip()
    if "news.ycombinator.com/item?id=" in entry_id:
        return entry_id
    return ""


def build_suggested_search_query(title: str, url: str) -> str:
    domain = re.sub(r"^www\.", "", urlparse(url).netloc)
    return normalize_space(f"{title} {domain}".strip())


def count_keyword(text: str, keyword: str) -> int:
    if not text or not keyword:
        return 0
    pattern = re.escape(keyword.lower())
    return len(re.findall(pattern, text.lower()))


def normalize_feedback_topic(topic: str) -> str:
    normalized = topic.lower().strip()
    for prefix in ("weakly related ", "unrelated ", "generic "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
    if normalized.endswith(" only"):
        normalized = normalized[:-5].strip()
    return normalized


def topic_matches(text: str, topic: str) -> bool:
    if not text or not topic:
        return False
    text_lower = text.lower()
    topic_lower = topic.lower().strip()
    normalized_topic = normalize_feedback_topic(topic)
    if topic_lower in text_lower or normalized_topic in text_lower:
        return True

    words = [word for word in re.findall(r"[a-z0-9]+", normalized_topic) if len(word) > 2]
    return bool(words) and all(word in text_lower for word in words)


def source_matches(source: str, preferred_source: str) -> bool:
    return preferred_source.lower().strip() in source.lower()


def apply_feedback_score(
    title: str,
    source: str,
    summary: str,
    body: str,
    keyword_score: int,
    feedback: dict[str, Any],
) -> tuple[int, int, list[str]]:
    text = " ".join([title, summary, body])
    feedback_score = 0
    reasons: list[str] = []

    for topic in feedback.get("likes", {}).get("topics", []) or []:
        if topic_matches(text, str(topic)):
            feedback_score += LIKE_TOPIC_WEIGHT
            reasons.append(f"like_topic:{topic}")

    for preferred_source in feedback.get("likes", {}).get("sources", []) or []:
        if source_matches(source, str(preferred_source)):
            feedback_score += LIKE_SOURCE_WEIGHT
            reasons.append(f"like_source:{preferred_source}")

    for topic in feedback.get("dislikes", {}).get("topics", []) or []:
        if topic_matches(text, str(topic)):
            feedback_score += DISLIKE_TOPIC_WEIGHT
            reasons.append(f"dislike_topic:{topic}")

    for disliked_source in feedback.get("dislikes", {}).get("sources", []) or []:
        if source_matches(source, str(disliked_source)):
            feedback_score += DISLIKE_SOURCE_WEIGHT
            reasons.append(f"dislike_source:{disliked_source}")

    adjusted_score = keyword_score + feedback_score
    return feedback_score, adjusted_score, reasons


def score_item(title: str, summary: str, body: str, keywords: dict[str, list[str]]) -> tuple[int, list[str]]:
    weights = {
        "high_priority": 5,
        "medium_priority": 2,
        "low_priority": 1,
    }
    fields = [
        (title, 3),
        (summary, 2),
        (body, 1),
    ]

    total = 0
    matched: set[str] = set()
    for group, keyword_list in keywords.items():
        keyword_weight = weights.get(group, 1)
        for keyword in keyword_list or []:
            hits = sum(count_keyword(text, keyword) * field_weight for text, field_weight in fields)
            if hits:
                total += hits * keyword_weight
                matched.add(keyword)

    return total, sorted(matched, key=str.lower)


def build_candidates(config: dict[str, Any], feedback: dict[str, Any], conn: sqlite3.Connection) -> list[NewsItem]:
    max_items = int(config.get("max_items_per_source", 8))
    max_candidates = int(config.get("max_candidates", 30))
    fetch_full_text = bool(config.get("fetch_full_text", True))
    keywords = config.get("keywords", {})

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "personal-news-assistant/0.1 (+https://example.local)",
        }
    )

    candidates: list[NewsItem] = []
    for source in config.get("rss_sources", []):
        source_name = source.get("name", "Unnamed source")
        entries = fetch_rss_entries(source, max_items, session)
        logging.info("Fetched %s RSS entries from %s.", len(entries), source_name)

        for entry in entries:
            title = normalize_space(strip_html(entry.get("title", "")))
            url = entry.get("link", "").strip()
            if not title or not url:
                continue
            if is_processed(conn, url):
                logging.info("Skipped already processed news: %s", url)
                continue

            summary = normalize_space(strip_html(entry.get("summary", "") or entry.get("description", "")))
            url_status, extracted_body = fetch_article_content(url, session)
            body = extracted_body if fetch_full_text else ""
            content_length = len(extracted_body)
            invalid_link = is_invalid_link(url_status)
            weak_content = is_success_status(url_status) and content_length < MIN_CONTENT_LENGTH
            content_available = is_success_status(url_status) and not weak_content
            fallback_url = get_hacker_news_fallback(entry) if source_name.lower() == "hacker news" else ""
            suggested_search_query = build_suggested_search_query(title, url)
            score, matched_keywords = score_item(title, summary, body, keywords)
            if score <= 0:
                continue
            feedback_score, adjusted_score, feedback_reasons = apply_feedback_score(
                title,
                source_name,
                summary,
                body,
                score,
                feedback,
            )

            logging.info(
                "Candidate validation: title=%r source=%s url=%s url_status=%s content_length=%s "
                "content_available=%s invalid_link=%s weak_content=%s keyword_score=%s "
                "feedback_score=%s adjusted_score=%s feedback_reasons=%s fallback_url=%s",
                title,
                source_name,
                url,
                url_status,
                content_length,
                content_available,
                invalid_link,
                weak_content,
                score,
                feedback_score,
                adjusted_score,
                ",".join(feedback_reasons) if feedback_reasons else "none",
                fallback_url or "",
            )

            candidates.append(
                NewsItem(
                    title=title,
                    source=source_name,
                    url=url,
                    summary=summary,
                    body=body,
                    score=score,
                    matched_keywords=matched_keywords,
                    feedback_score=feedback_score,
                    adjusted_score=adjusted_score,
                    feedback_reasons=feedback_reasons,
                    url_status=url_status,
                    content_length=content_length,
                    content_available=content_available,
                    invalid_link=invalid_link,
                    weak_content=weak_content,
                    suggested_search_query=suggested_search_query,
                    fallback_url=fallback_url,
                    published=entry.get("published", "") or entry.get("updated", ""),
                )
            )

    candidates.sort(key=lambda item: item.adjusted_score, reverse=True)
    candidates = candidates[:max_candidates]
    invalid_count = sum(1 for item in candidates if item.invalid_link)
    weak_count = sum(1 for item in candidates if item.weak_content)
    logging.info(
        "Candidate quality summary: total_candidates=%s invalid_link=%s weak_content=%s",
        len(candidates),
        invalid_count,
        weak_count,
    )
    return candidates


def filter_llm_candidates(candidates: list[NewsItem]) -> list[NewsItem]:
    llm_candidates = [
        item
        for item in candidates
        if item.content_available
        and not item.invalid_link
        and not item.weak_content
        and item.adjusted_score >= MIN_ADJUSTED_SCORE
    ]
    logging.info("Final LLM candidate count: %s", len(llm_candidates))
    return llm_candidates


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def format_recent_feedback(feedback: dict[str, Any], limit: int = 7) -> list[str]:
    daily_feedback = feedback.get("daily_feedback", []) or []
    recent_feedback = daily_feedback[-limit:]
    lines: list[str] = []
    for item in recent_feedback:
        lines.append(f"- date: {item.get('date', 'unknown')}")
        for key in ("liked", "disliked", "issues", "notes"):
            values = item.get(key) or []
            if values:
                lines.append(f"  {key}: {', '.join(str(value) for value in values)}")
    return lines


def build_llm_prompt(config: dict[str, Any], feedback: dict[str, Any], candidates: list[NewsItem]) -> str:
    final_top_n = int(config.get("final_top_n", 8))
    user_profile = config.get("user_profile", "")
    learning_style = config.get("learning_style", "")
    learning_preferences = feedback.get("learning_preferences", []) or []
    recent_feedback_lines = format_recent_feedback(feedback)

    lines = [
        "请从下面候选新闻中选择最值得我今天阅读的新闻，并生成中文 Markdown 日报。",
        "",
        f"候选新闻总数是 {len(candidates)} 条。最多选择 {min(final_top_n, len(candidates))} 条。",
        "只能输出下面候选列表中明确出现的新闻标题，不得新增候选外新闻，不得把背景材料改写成额外新闻条目。",
        "每条新闻必须保留原文链接，并且只能基于候选新闻给出的信息总结。",
        "如果原文、摘要或正文信息不足，必须明确写“原文信息有限”，不能编造背景、数据或结论。",
        "候选新闻包含 url_status、content_length、content_available。你必须根据这些字段判断可读性。",
        "如果 content_available=false，不得编造正文细节，不得生成具体技术内容总结。",
        "如果 url_status 不是 2xx，不得推荐为“值得深入阅读”。",
        "如果只有标题和 RSS 摘要，只能说“信息不足，需人工确认”。",
        "候选新闻里的 keyword_score 是内部关键词排序分数，只能作为参考，不要直接展示为最终相关性评分。",
        "最终 Markdown 中的“相关性评分”必须由你根据用户背景和新闻价值重新判断，输出 1-10 的整数或一位小数。",
        "如果某条新闻涉及用户可能不熟悉的概念、机构、技术路线或论文方法，请先用 3-5 句话解释前置背景，再进入结论和行动建议。",
        "不要把“勉强相关”的新闻硬说成强相关。如果只是弱相关，必须明确写“弱相关，可跳过”。",
        "如果候选整体相关性较低，生成“今日无高价值新闻”，不要硬凑日报。",
        "",
        "我的背景：",
        str(user_profile),
        "",
        "我的学习风格：",
        str(learning_style),
        "",
        "我的长期反馈偏好：",
        *(f"- {preference}" for preference in learning_preferences),
        "",
        "最近 7 条 daily_feedback：",
        *(recent_feedback_lines or ["- 无"]),
        "",
        "每条摘要必须使用以下结构：",
        "## 标题",
        "- 来源：",
        "- 原文链接：",
        "- 链接状态：",
        "- 正文抓取状态：",
        "- 相关性评分：1-10 分，不要使用 keyword_score 原值",
        "- 前置背景：如果涉及陌生概念，用 3-5 句话解释；如果不需要，写“无需额外前置背景”",
        "- 为什么推荐：",
        "- 为什么可能不推荐：",
        "- 一句话结论：",
        "- 你需要知道：",
        "- 和我有什么关系：",
        "- 建议行动：",
        "",
        "候选新闻：",
    ]

    for index, item in enumerate(candidates, start=1):
        text = item.body or item.summary or ""
        if not text:
            text = "原文信息有限"
        lines.extend(
            [
                f"\n### 候选 {index}",
                f"标题：{item.title}",
                f"来源：{item.source}",
                f"原文链接：{item.url}",
                f"Hacker News fallback 链接：{item.fallback_url or '无'}",
                f"发布时间：{item.published or '未知'}",
                f"url_status：{item.url_status}",
                f"content_length：{item.content_length}",
                f"content_available：{str(item.content_available).lower()}",
                f"invalid_link：{str(item.invalid_link).lower()}",
                f"weak_content：{str(item.weak_content).lower()}",
                f"suggested_search_query：{item.suggested_search_query}",
                f"keyword_score（内部关键词排序分数，仅供参考）：{item.score}",
                f"feedback_score（来自 feedback.yaml 的加减分）：{item.feedback_score}",
                f"adjusted_score（排序使用）：{item.adjusted_score}",
                f"feedback_reasons：{', '.join(item.feedback_reasons) if item.feedback_reasons else '无'}",
                f"命中关键词：{', '.join(item.matched_keywords) if item.matched_keywords else '无'}",
                f"RSS 摘要：{truncate(item.summary, 400) or '原文信息有限'}",
                f"正文摘录：{truncate(text, 900)}",
            ]
        )

    return "\n".join(lines)


def generate_markdown(config: dict[str, Any], feedback: dict[str, Any], candidates: list[NewsItem]) -> str:
    api_key, base_url, model = load_llm_settings()
    client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = build_llm_prompt(config, feedback, candidates)
    logging.info("Sending %s candidates to LLM. Prompt length: %s chars.", len(candidates), len(prompt))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是一个谨慎的中文新闻摘要助手。你重视事实边界、原文链接、"
                    "学习价值和对用户当前项目的相关性。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned an empty response.")
    return content.strip()


def build_archive_path(archive_dir: Path, now: datetime) -> Path:
    archive_time = now
    while True:
        archive_path = archive_dir / f"daily_news_{archive_time.strftime('%Y-%m-%d_%H%M%S')}.md"
        if not archive_path.exists():
            return archive_path
        archive_time += timedelta(seconds=1)


def write_output(
    config: dict[str, Any],
    markdown_body: str,
    candidates_count: int,
    update_daily: bool = True,
) -> dict[str, Path]:
    output_dir = ROOT_DIR / str(config.get("output_dir", "outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = output_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    latest_path = output_dir / "latest_daily_news.md"
    daily_path = output_dir / f"daily_news_{today}.md"
    archive_path = build_archive_path(archive_dir, now)

    header = "\n".join(
        [
            f"# 每日 News 助手 - {today}",
            "",
            f"- 生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 候选新闻数：{candidates_count}",
            "",
        ]
    )
    content = header + markdown_body + "\n"
    latest_path.write_text(content, encoding="utf-8")
    if update_daily:
        daily_path.write_text(content, encoding="utf-8")
    else:
        logging.info("Skipped daily output overwrite for low-value report: %s", daily_path)
    archive_path.write_text(content, encoding="utf-8")

    output_paths = {
        "latest": latest_path,
        "daily": daily_path,
        "archive": archive_path,
    }
    for label, path in output_paths.items():
        if label == "daily" and not update_daily:
            continue
        logging.info("Saved %s output: %s", label, path)
    return output_paths


def describe_content_status(item: NewsItem) -> str:
    if item.invalid_link:
        return "链接不可用，需人工确认"
    if item.weak_content:
        return "正文抓取不足，需人工打开确认"
    if item.content_available:
        return f"正文可读，长度 {item.content_length} 字符"
    return "正文不可读，需人工确认"


def build_empty_report(config: dict[str, Any], rejected_candidates: list[NewsItem] | None = None) -> dict[str, Path]:
    lines = [
        "今日无高价值新闻。",
        "",
        "原因可能是候选新闻链接不可用、正文抓取不足，或今天没有命中兴趣关键词且未处理过的新闻。",
        "请查看 logs/run.log 中的 url_status、content_length、invalid_link 和 weak_content 记录。",
    ]

    if rejected_candidates:
        lines.extend(["", "## 需人工确认的候选", ""])
        for item in rejected_candidates[:10]:
            lines.extend(
                [
                    f"### {item.title}",
                    f"- 来源：{item.source}",
                    f"- 原文链接：{item.url}",
                    f"- 链接状态：{item.url_status}",
                    f"- 正文抓取状态：{describe_content_status(item)}",
                    f"- 建议搜索：{item.suggested_search_query}",
                ]
            )
            if item.fallback_url:
                lines.append(f"- Hacker News fallback：{item.fallback_url}")
            lines.append("")

    markdown = "\n".join(lines)
    return write_output(config, markdown, candidates_count=0, update_daily=False)


def main() -> None:
    setup_logging()
    logging.info("Daily news assistant started.")

    config = load_config()
    feedback = load_feedback()
    conn = init_db(str(config.get("db_path", "news.db")))
    try:
        candidates = build_candidates(config, feedback, conn)
        logging.info("Built %s candidates.", len(candidates))
        llm_candidates = filter_llm_candidates(candidates)

        if not llm_candidates:
            output_paths = build_empty_report(config, candidates)
        else:
            markdown = generate_markdown(config, feedback, llm_candidates)
            output_paths = write_output(config, markdown, candidates_count=len(llm_candidates))
            mark_processed(conn, llm_candidates)

        logging.info("Daily news assistant finished. Outputs: %s", output_paths)
        print(f"Latest output written to: {output_paths['latest'].resolve()}")
        print(f"Daily output path: {output_paths['daily'].resolve()}")
        print(f"Archive output written to: {output_paths['archive'].resolve()}")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        setup_logging()
        logging.exception("Daily news assistant failed: %s", exc)
        print(f"Run failed: {exc}")
        raise
