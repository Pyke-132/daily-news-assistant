from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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


@dataclass
class ReportItem:
    title: str
    source: str
    url: str
    published: str
    relevance_score: float
    link_status: str
    content_status: str
    front_context: str
    why_recommend: str
    why_not_recommend: str
    one_sentence: str
    original_facts: list[str]
    model_summary: list[str]
    related_thoughts: list[str]
    action: str
    matched_keywords: list[str]


@dataclass
class RunReport:
    schema_version: int
    report_type: str
    report_date: str
    generated_at: str
    status: str
    candidate_count: int
    items: list[ReportItem]
    optional_references: list[ReportItem]
    low_value_reason: str = ""
    error_stage: str = ""
    error_message: str = ""


@dataclass
class DailyReport:
    schema_version: int
    report_type: str
    report_date: str
    updated_at: str
    items: list[ReportItem]


SCHEMA_VERSION = 1
RUN_STATUSES = {"formal", "low_value", "failed"}
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
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


def local_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_llm_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM JSON must be an object.")
    return data


def require_field(data: dict[str, Any], field_name: str) -> Any:
    if field_name not in data:
        raise ValueError(f"Missing required field: {field_name}")
    return data[field_name]


def require_string(data: dict[str, Any], field_name: str, allow_empty: bool = False) -> str:
    value = require_field(data, field_name)
    if not isinstance(value, str):
        raise ValueError(f"Field {field_name} must be a string.")
    if not allow_empty and not value.strip():
        raise ValueError(f"Field {field_name} must not be empty.")
    return value.strip()


def require_string_list(data: dict[str, Any], field_name: str) -> list[str]:
    value = require_field(data, field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Field {field_name} must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


def require_item_list(data: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = require_field(data, field_name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Field {field_name} must be a list of objects.")
    return value


def looks_like_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def report_item_from_dict(data: dict[str, Any]) -> ReportItem:
    if not isinstance(data, dict):
        raise ValueError("Report item must be an object.")
    score = require_field(data, "relevance_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("Field relevance_score must be a number.")
    score_float = float(score)
    if not 1 <= score_float <= 10:
        raise ValueError("Field relevance_score must be between 1 and 10.")

    url = require_string(data, "url")
    if not looks_like_http_url(url):
        raise ValueError(f"Report item URL must be http/https: {url}")

    return ReportItem(
        title=require_string(data, "title"),
        source=require_string(data, "source"),
        url=url,
        published=require_string(data, "published", allow_empty=True),
        relevance_score=score_float,
        link_status=require_string(data, "link_status"),
        content_status=require_string(data, "content_status"),
        front_context=require_string(data, "front_context"),
        why_recommend=require_string(data, "why_recommend"),
        why_not_recommend=require_string(data, "why_not_recommend"),
        one_sentence=require_string(data, "one_sentence"),
        original_facts=require_string_list(data, "original_facts"),
        model_summary=require_string_list(data, "model_summary"),
        related_thoughts=require_string_list(data, "related_thoughts"),
        action=require_string(data, "action"),
        matched_keywords=require_string_list(data, "matched_keywords"),
    )


def validate_report_items_against_candidates(items: list[ReportItem], candidates: list[NewsItem], field_name: str) -> None:
    candidate_pairs = {(item.title, item.url) for item in candidates}
    candidate_titles = {item.title for item in candidates}
    candidate_urls = {item.url for item in candidates}
    for item in items:
        if (item.title, item.url) not in candidate_pairs:
            if item.title not in candidate_titles:
                raise ValueError(f"{field_name} contains title outside candidates: {item.title}")
            if item.url not in candidate_urls:
                raise ValueError(f"{field_name} contains URL outside candidates: {item.url}")
            raise ValueError(f"{field_name} contains mismatched candidate title and URL: {item.title}")


def validate_run_report(data: dict[str, Any], candidates: list[NewsItem]) -> RunReport:
    if not isinstance(data, dict):
        raise ValueError("Run report must be an object.")
    schema_version = require_field(data, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version: {schema_version}")
    report_type = require_string(data, "report_type")
    if report_type != "run":
        raise ValueError(f"Run report_type must be 'run', got {report_type!r}.")
    status = require_string(data, "status")
    if status not in RUN_STATUSES:
        raise ValueError(f"Invalid run status: {status}")
    report_date = require_string(data, "report_date")
    generated_at = require_string(data, "generated_at")
    candidate_count = require_field(data, "candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or candidate_count < 0:
        raise ValueError("Field candidate_count must be a non-negative integer.")

    items = [report_item_from_dict(item) for item in require_item_list(data, "items")]
    optional_references = [
        report_item_from_dict(item) for item in require_item_list(data, "optional_references")
    ]
    low_value_reason = str(data.get("low_value_reason", "") or "").strip()
    error_stage = str(data.get("error_stage", "") or "").strip()
    error_message = str(data.get("error_message", "") or "").strip()

    if status == "formal" and not items:
        raise ValueError("Formal report must contain at least one item.")
    if status == "low_value":
        if items:
            raise ValueError("Low-value report must not contain formal items.")
        if not low_value_reason:
            raise ValueError("Low-value report must include low_value_reason.")
    if status == "failed" and (items or optional_references):
        raise ValueError("Failed report must not contain items or optional references.")

    validate_report_items_against_candidates(items, candidates, "items")
    validate_report_items_against_candidates(optional_references, candidates, "optional_references")

    return RunReport(
        schema_version=schema_version,
        report_type=report_type,
        report_date=report_date,
        generated_at=generated_at,
        status=status,
        candidate_count=candidate_count,
        items=items,
        optional_references=optional_references,
        low_value_reason=low_value_reason,
        error_stage=error_stage,
        error_message=error_message,
    )


def report_item_to_dict(item: ReportItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "source": item.source,
        "url": item.url,
        "published": item.published,
        "relevance_score": item.relevance_score,
        "link_status": item.link_status,
        "content_status": item.content_status,
        "front_context": item.front_context,
        "why_recommend": item.why_recommend,
        "why_not_recommend": item.why_not_recommend,
        "one_sentence": item.one_sentence,
        "original_facts": list(item.original_facts),
        "model_summary": list(item.model_summary),
        "related_thoughts": list(item.related_thoughts),
        "action": item.action,
        "matched_keywords": list(item.matched_keywords),
    }


def run_report_to_dict(report: RunReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "report_type": report.report_type,
        "report_date": report.report_date,
        "generated_at": report.generated_at,
        "status": report.status,
        "candidate_count": report.candidate_count,
        "items": [report_item_to_dict(item) for item in report.items],
        "optional_references": [report_item_to_dict(item) for item in report.optional_references],
        "low_value_reason": report.low_value_reason,
        "error_stage": report.error_stage,
        "error_message": report.error_message,
    }


def daily_report_to_dict(report: DailyReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "report_type": report.report_type,
        "report_date": report.report_date,
        "updated_at": report.updated_at,
        "items": [report_item_to_dict(item) for item in report.items],
    }


def normalize_url_for_dedupe(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower.startswith("utm_") or key_lower in TRACKING_QUERY_KEYS:
            continue
        query_pairs.append((key, value))
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def load_daily_report(path: Path) -> DailyReport | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Daily JSON is malformed: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Daily JSON must be an object: {path}")
    schema_version = require_field(data, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported daily schema_version: {schema_version}")
    report_type = require_string(data, "report_type")
    if report_type != "daily":
        raise ValueError(f"Daily report_type must be 'daily', got {report_type!r}.")
    return DailyReport(
        schema_version=schema_version,
        report_type=report_type,
        report_date=require_string(data, "report_date"),
        updated_at=require_string(data, "updated_at"),
        items=[report_item_from_dict(item) for item in require_item_list(data, "items")],
    )


def merge_daily_items(old_items: list[ReportItem], new_items: list[ReportItem]) -> list[ReportItem]:
    merged = list(old_items)
    seen = {normalize_url_for_dedupe(item.url) for item in old_items}
    for item in new_items:
        normalized_url = normalize_url_for_dedupe(item.url)
        if normalized_url in seen:
            continue
        merged.append(item)
        seen.add(normalized_url)
    return merged


def markdown_list(values: list[str]) -> list[str]:
    return [f"  - {value}" for value in values] if values else ["  - 无"]


def render_report_item_markdown(item: ReportItem, index: int) -> list[str]:
    lines = [
        f"## {index}. {item.title}",
        "",
        f"- 来源：{item.source}",
        f"- 原文链接：[{item.url}]({item.url})",
        f"- 发布时间：{item.published or '未知'}",
        f"- 链接状态：{item.link_status}",
        f"- 正文状态：{item.content_status}",
        f"- 相关性评分：{item.relevance_score:g} / 10",
        f"- 前置背景：{item.front_context}",
        f"- 为什么推荐：{item.why_recommend}",
        f"- 为什么可能不推荐：{item.why_not_recommend}",
        f"- 一句话结论：{item.one_sentence}",
        "",
        "### 原文事实",
        *markdown_list(item.original_facts),
        "",
        "### 模型总结",
        *markdown_list(item.model_summary),
        "",
        "### 与用户的关联思考",
        *markdown_list(item.related_thoughts),
        "",
        f"- 建议行动：{item.action}",
        f"- 命中关键词：{', '.join(item.matched_keywords) if item.matched_keywords else '无'}",
        "",
    ]
    return lines


def render_run_markdown(report: RunReport) -> str:
    lines = [
        f"# 每日 News 助手 - 本次运行 - {report.report_date}",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 运行状态：{report.status}",
        f"- 候选新闻数：{report.candidate_count}",
        f"- 正式新闻数：{len(report.items)}",
        "",
    ]
    if report.status == "failed":
        lines.extend(
            [
                "## 本次运行失败",
                "",
                f"- 失败阶段：{report.error_stage or 'unknown'}",
                f"- 错误信息：{report.error_message or 'unknown'}",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"
    if report.status == "low_value":
        lines.extend(
            [
                "## 今日无高价值新闻",
                "",
                report.low_value_reason or "本次运行没有达到正式日报门槛的新闻。",
                "",
            ]
        )
        if report.optional_references:
            lines.extend(["## 可选参考", ""])
            for index, item in enumerate(report.optional_references, start=1):
                lines.extend(render_report_item_markdown(item, index))
        return "\n".join(lines).rstrip() + "\n"

    for index, item in enumerate(report.items, start=1):
        lines.extend(render_report_item_markdown(item, index))
    if report.optional_references:
        lines.extend(["# 可选参考", ""])
        for index, item in enumerate(report.optional_references, start=1):
            lines.extend(render_report_item_markdown(item, index))
    return "\n".join(lines).rstrip() + "\n"


def render_daily_markdown(report: DailyReport) -> str:
    lines = [
        f"# 每日 News 助手 - 当天累计日报 - {report.report_date}",
        "",
        f"- 更新时间：{report.updated_at}",
        f"- 累计正式新闻数：{len(report.items)}",
        "",
    ]
    if not report.items:
        lines.append("今日暂无累计正式新闻。")
        return "\n".join(lines).rstrip() + "\n"
    for index, item in enumerate(report.items, start=1):
        lines.extend(render_report_item_markdown(item, index))
    return "\n".join(lines).rstrip() + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def build_failed_report(
    error_stage: str,
    error_message: str,
    report_date: str,
    generated_at: str,
    candidate_count: int,
) -> RunReport:
    return RunReport(
        schema_version=SCHEMA_VERSION,
        report_type="run",
        report_date=report_date,
        generated_at=generated_at,
        status="failed",
        candidate_count=candidate_count,
        items=[],
        optional_references=[],
        error_stage=error_stage,
        error_message=error_message,
    )


def build_low_value_report(
    report_date: str,
    generated_at: str,
    candidate_count: int,
    reason: str,
) -> RunReport:
    return RunReport(
        schema_version=SCHEMA_VERSION,
        report_type="run",
        report_date=report_date,
        generated_at=generated_at,
        status="low_value",
        candidate_count=candidate_count,
        items=[],
        optional_references=[],
        low_value_reason=reason,
    )


def build_archive_paths(output_dir: Path, now: datetime) -> tuple[Path, Path]:
    archive_dir = output_dir / "archive"
    archive_time = now
    while True:
        stem = f"daily_news_{archive_time.strftime('%Y-%m-%d_%H%M%S')}"
        json_path = archive_dir / f"{stem}.json"
        md_path = archive_dir / f"{stem}.md"
        if not json_path.exists() and not md_path.exists():
            return json_path, md_path
        archive_time += timedelta(seconds=1)


def write_run_outputs(output_dir: Path, report: RunReport, now: datetime | None = None) -> dict[str, Path]:
    now = now or datetime.now()
    latest_json_path = output_dir / "latest_daily_news.json"
    latest_md_path = output_dir / "latest_daily_news.md"
    archive_json_path, archive_md_path = build_archive_paths(output_dir, now)
    report_data = run_report_to_dict(report)
    markdown = render_run_markdown(report)

    write_json_atomic(latest_json_path, report_data)
    write_text_atomic(latest_md_path, markdown)
    write_json_atomic(archive_json_path, report_data)
    write_text_atomic(archive_md_path, markdown)

    return {
        "latest_json": latest_json_path,
        "latest_md": latest_md_path,
        "archive_json": archive_json_path,
        "archive_md": archive_md_path,
    }


def write_daily_outputs(output_dir: Path, report: RunReport, now: datetime | None = None) -> dict[str, Path]:
    if report.status != "formal":
        return {}
    now = now or datetime.now()
    daily_json_path = output_dir / f"daily_news_{report.report_date}.json"
    daily_md_path = output_dir / f"daily_news_{report.report_date}.md"
    existing = load_daily_report(daily_json_path)
    if existing is not None and existing.report_date != report.report_date:
        raise ValueError(
            f"Existing daily report date {existing.report_date} does not match run date {report.report_date}."
        )
    merged_items = merge_daily_items(existing.items if existing else [], report.items)
    daily_report = DailyReport(
        schema_version=SCHEMA_VERSION,
        report_type="daily",
        report_date=report.report_date,
        updated_at=now.isoformat(timespec="seconds"),
        items=merged_items,
    )
    write_json_atomic(daily_json_path, daily_report_to_dict(daily_report))
    write_text_atomic(daily_md_path, render_daily_markdown(daily_report))
    return {"daily_json": daily_json_path, "daily_md": daily_md_path}


def select_processed_candidates(report: RunReport, candidates: list[NewsItem]) -> list[NewsItem]:
    if report.status != "formal":
        return []
    candidate_by_url = {normalize_url_for_dedupe(item.url): item for item in candidates}
    selected: list[NewsItem] = []
    seen: set[str] = set()
    for item in report.items:
        normalized_url = normalize_url_for_dedupe(item.url)
        candidate = candidate_by_url.get(normalized_url)
        if candidate and normalized_url not in seen:
            selected.append(candidate)
            seen.add(normalized_url)
    return selected


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


def build_llm_prompt(
    config: dict[str, Any],
    feedback: dict[str, Any],
    candidates: list[NewsItem],
    report_date: str | None = None,
    generated_at: str | None = None,
) -> str:
    final_top_n = int(config.get("final_top_n", 8))
    user_profile = config.get("user_profile", "")
    learning_style = config.get("learning_style", "")
    learning_preferences = feedback.get("learning_preferences", []) or []
    recent_feedback_lines = format_recent_feedback(feedback)
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    generated_at = generated_at or local_timestamp()

    lines = [
        "请从下面候选新闻中选择最值得我今天阅读的新闻，并只返回一个 JSON object。",
        "不要输出 Markdown，不要使用代码围栏，不要在 JSON 前后添加解释文字。",
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
        "relevance_score 必须由你根据用户背景和新闻价值重新判断，输出 1-10 的数字，不得直接使用 keyword_score。",
        "如果某条新闻涉及用户可能不熟悉的概念、机构、技术路线或论文方法，请先用 3-5 句话解释前置背景，再进入结论和行动建议。",
        "不要把“勉强相关”的新闻硬说成强相关。如果只是弱相关，必须明确写“弱相关，可跳过”。",
        "如果候选整体相关性较低，返回 status=low_value，不要硬凑日报。",
        "status 只能是 formal 或 low_value。failed 是程序诊断状态，禁止你返回 failed。",
        "",
        "JSON 顶层字段必须完整包含：",
        "{",
        f'  "schema_version": {SCHEMA_VERSION},',
        '  "report_type": "run",',
        f'  "report_date": "{report_date}",',
        f'  "generated_at": "{generated_at}",',
        '  "status": "formal 或 low_value",',
        '  "candidate_count": 数字,',
        '  "items": [],',
        '  "optional_references": [],',
        '  "low_value_reason": "",',
        '  "error_stage": "",',
        '  "error_message": ""',
        "}",
        "",
        "formal 规则：items 至少 1 条；optional_references 可以为空。",
        "low_value 规则：items 必须为空；必须填写 low_value_reason；optional_references 可以放低优先级或需人工确认的候选。",
        "",
        "items 和 optional_references 中每个对象必须完整包含：",
        "title, source, url, published, relevance_score, link_status, content_status, front_context,",
        "why_recommend, why_not_recommend, one_sentence, original_facts, model_summary, related_thoughts, action, matched_keywords。",
        "original_facts 只能写原文明确事实；model_summary 写提炼总结；related_thoughts 写和用户背景/项目的关联思考。",
        "original_facts、model_summary、related_thoughts、matched_keywords 必须是字符串数组。",
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


def generate_run_report(
    config: dict[str, Any],
    feedback: dict[str, Any],
    candidates: list[NewsItem],
    report_date: str,
    generated_at: str,
) -> RunReport:
    api_key, base_url, model = load_llm_settings()
    client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = build_llm_prompt(config, feedback, candidates, report_date, generated_at)
    logging.info("Sending %s candidates to LLM. Prompt length: %s chars.", len(candidates), len(prompt))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是一个谨慎的中文新闻摘要助手。你只输出严格 JSON，重视事实边界、"
                    "原文链接、学习价值和对用户当前项目的相关性。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned an empty response.")
    data = parse_llm_json(content)
    if data.get("status") == "failed":
        raise ValueError("LLM must not return failed status.")
    return validate_run_report(data, candidates)


def main() -> None:
    setup_logging()
    logging.info("Daily news assistant started.")

    config = load_config()
    feedback = load_feedback()
    conn = init_db(str(config.get("db_path", "news.db")))
    try:
        now = datetime.now()
        report_date = now.strftime("%Y-%m-%d")
        generated_at = now.isoformat(timespec="seconds")
        output_dir = ROOT_DIR / str(config.get("output_dir", "outputs"))
        candidates = build_candidates(config, feedback, conn)
        logging.info("Built %s candidates.", len(candidates))
        llm_candidates = filter_llm_candidates(candidates)

        if not llm_candidates:
            report = build_low_value_report(
                report_date=report_date,
                generated_at=generated_at,
                candidate_count=len(candidates),
                reason=(
                    "本次运行没有可进入正式日报的新闻。原因可能是候选新闻链接不可用、"
                    "正文抓取不足、相关性分数过低，或今天没有命中兴趣关键词且未处理过的新闻。"
                ),
            )
        else:
            try:
                report = generate_run_report(config, feedback, llm_candidates, report_date, generated_at)
            except Exception as exc:
                logging.exception("Failed to generate or validate LLM JSON: %s", exc)
                report = build_failed_report(
                    error_stage="llm_json",
                    error_message=str(exc),
                    report_date=report_date,
                    generated_at=generated_at,
                    candidate_count=len(llm_candidates),
                )

        try:
            output_paths = write_run_outputs(output_dir, report, now)
        except Exception as exc:
            logging.exception("Failed to write latest/archive outputs: %s", exc)
            raise

        if report.status == "formal":
            try:
                output_paths.update(write_daily_outputs(output_dir, report, now))
            except Exception as exc:
                logging.exception("Failed to merge or write daily cumulative report: %s", exc)
                failed_report = build_failed_report(
                    error_stage="daily_write",
                    error_message=str(exc),
                    report_date=report_date,
                    generated_at=local_timestamp(),
                    candidate_count=len(llm_candidates),
                )
                output_paths.update(write_run_outputs(output_dir, failed_report))
                report = failed_report
            else:
                processed_items = select_processed_candidates(report, llm_candidates)
                mark_processed(conn, processed_items)

        logging.info("Daily news assistant finished. Outputs: %s", output_paths)
        print(f"Run status: {report.status}")
        print(f"Latest JSON written to: {output_paths['latest_json'].resolve()}")
        print(f"Latest Markdown written to: {output_paths['latest_md'].resolve()}")
        if "daily_json" in output_paths:
            print(f"Daily JSON written to: {output_paths['daily_json'].resolve()}")
            print(f"Daily Markdown written to: {output_paths['daily_md'].resolve()}")
        print(f"Archive JSON written to: {output_paths['archive_json'].resolve()}")
        print(f"Archive Markdown written to: {output_paths['archive_md'].resolve()}")
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
