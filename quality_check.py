from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_MARKDOWN_PATH = ROOT_DIR / "outputs" / "latest_daily_news.md"
QUALITY_REPORT_PATH = ROOT_DIR / "outputs" / "quality" / "latest_quality_report.md"
INVALID_STATUS_CODES = {404, 410, 500, 502, 503, 504}
SUMMARY_FIELDS = ("一句话结论", "你需要知道", "建议行动")
REQUIRED_FIELDS = (
    "来源",
    "原文链接",
    "链接状态",
    "正文抓取状态",
    "相关性评分",
    "为什么推荐",
    "为什么可能不推荐",
)


@dataclass
class NewsItem:
    title: str
    raw: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.fields.get("来源", "")

    @property
    def url(self) -> str:
        return self.fields.get("原文链接", "")

    @property
    def declared_link_status(self) -> str:
        return self.fields.get("链接状态", "")

    @property
    def declared_content_status(self) -> str:
        return self.fields.get("正文抓取状态", "")

    @property
    def relevance_score(self) -> str:
        return self.fields.get("相关性评分", "")

    @property
    def why_recommend(self) -> str:
        return self.fields.get("为什么推荐", "")

    @property
    def why_not_recommend(self) -> str:
        return self.fields.get("为什么可能不推荐", "")


@dataclass
class LinkCheck:
    url: str
    status_code: int | None = None
    final_url: str = ""
    content_type: str = ""
    error: str = ""


@dataclass
class Issue:
    level: str
    title: str
    issue_type: str
    message: str


@dataclass
class QualityReport:
    markdown_path: Path
    low_value_report: bool
    item_count: int
    issues: list[Issue]
    link_checks: dict[str, LinkCheck]

    @property
    def item_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.title != "日报整体"]

    @property
    def overall_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.title == "日报整体"]

    @property
    def pass_items(self) -> int:
        issue_titles = {issue.title for issue in self.item_issues}
        return max(0, self.item_count - len(issue_titles))

    @property
    def warn_items(self) -> int:
        titles = {issue.title for issue in self.item_issues if issue.level == "WARN"}
        fail_titles = {issue.title for issue in self.item_issues if issue.level == "FAIL"}
        return len(titles - fail_titles)

    @property
    def fail_items(self) -> int:
        return len({issue.title for issue in self.item_issues if issue.level == "FAIL"})

    @property
    def overall_warn_count(self) -> int:
        return len([issue for issue in self.overall_issues if issue.level == "WARN"])

    @property
    def overall_fail_count(self) -> int:
        return len([issue for issue in self.overall_issues if issue.level == "FAIL"])


def load_markdown(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Markdown file is empty: {path}")
    return text


def normalize_title(title: str) -> str:
    title = title.strip()
    title = re.sub(r"^\d+[\.\)、:：]\s*", "", title)
    return title.strip()


def is_summary_heading(title: str) -> bool:
    normalized = normalize_title(title).lower()
    ignored = (
        "今日 ai 日报",
        "今日ai日报",
        "今日无高价值新闻",
        "需人工确认的候选",
        "总结",
        "summary",
    )
    return any(text in normalized for text in ignored)


def split_heading_sections(markdown: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        title = normalize_title(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.append((title, markdown[start:end].strip()))
    return sections


def parse_field_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    stripped = re.sub(r"^[-*]\s*", "", stripped)
    stripped = re.sub(r"^\*\*(.+?)\*\*\s*[:：]", r"\1：", stripped)
    match = re.match(r"^([^:：]{1,30})[:：]\s*(.*)$", stripped)
    if not match:
        return None
    key = match.group(1).strip().strip("*").strip()
    value = match.group(2).strip()
    return key, value


def extract_markdown_url(value: str) -> str:
    markdown_link = re.search(r"\((https?://[^)\s]+)\)", value)
    if markdown_link:
        return markdown_link.group(1).strip()
    plain_url = re.search(r"https?://[^\s)>]+", value)
    if plain_url:
        return plain_url.group(0).strip()
    return value.strip()


def parse_news_items(markdown: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    for title, body in split_heading_sections(markdown):
        if is_summary_heading(title):
            continue

        fields: dict[str, str] = {}
        for line in body.splitlines():
            parsed = parse_field_line(line)
            if not parsed:
                continue
            key, value = parsed
            if key == "原文链接":
                value = extract_markdown_url(value)
            fields[key] = value

        # Treat sections without core news fields as prose rather than news items.
        if not any(key in fields for key in ("来源", "原文链接", "链接状态", "正文抓取状态")):
            continue
        items.append(NewsItem(title=title, raw=body, fields=fields))
    return items


def is_low_value_report(markdown: str) -> bool:
    patterns = ("今日无高价值新闻", "今日无高质量可读新闻", "无高价值新闻", "无高质量新闻")
    return any(pattern in markdown for pattern in patterns)


def check_low_value_report(markdown: str, items: list[NewsItem]) -> list[Issue]:
    issues: list[Issue] = []
    if is_low_value_report(markdown) and items:
        optional_reference = "可选参考" in markdown or any("可选参考" in item.title or "可选参考" in item.raw for item in items)
        issue_type = "low_value_with_optional_references" if optional_reference else "low_value_with_items"
        message = (
            "日报声明今日无高价值新闻，但包含明确标记为可选参考的条目。"
            if optional_reference
            else "日报声明今日无高价值新闻，但仍包含正式新闻条目。"
        )
        issues.append(
            Issue(
                "WARN",
                "日报整体",
                issue_type,
                message,
            )
        )
    return issues


def check_required_fields(items: list[NewsItem]) -> list[Issue]:
    issues: list[Issue] = []
    for item in items:
        for field_name in REQUIRED_FIELDS:
            if not item.fields.get(field_name, "").strip():
                issues.append(
                    Issue(
                        "FAIL",
                        item.title,
                        "missing_required_field",
                        f"缺少必填字段：{field_name}",
                    )
                )
    return issues


def looks_like_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def check_link_status(url: str, timeout: int = 8) -> LinkCheck:
    check = LinkCheck(url=url)
    headers = {"User-Agent": "personal-news-assistant-quality-check/0.1"}
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        if response.status_code in {405, 403}:
            raise requests.RequestException(f"HEAD returned {response.status_code}")
    except Exception as head_error:
        try:
            response = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers, stream=True)
        except Exception as get_error:
            check.error = f"HEAD failed: {head_error}; GET failed: {get_error}"
            return check

    check.status_code = response.status_code
    check.final_url = response.url
    check.content_type = response.headers.get("content-type", "")
    response.close()
    return check


def declared_as_accessible(status_text: str) -> bool:
    text = status_text.lower()
    return any(token in text for token in ("可访问", "成功", "200", "ok", "2xx", "正常"))


def declared_as_inaccessible(status_text: str) -> bool:
    text = status_text.lower()
    return any(token in text for token in ("不可访问", "打不开", "失败", "不足", "404", "410", "500", "502", "503", "504"))


def check_link_fields_match(items: list[NewsItem]) -> tuple[list[Issue], dict[str, LinkCheck]]:
    issues: list[Issue] = []
    link_checks: dict[str, LinkCheck] = {}
    seen: dict[str, str] = {}

    for item in items:
        url = item.url.strip()
        if not url:
            issues.append(Issue("FAIL", item.title, "empty_url", "原文链接为空。"))
            continue
        if not looks_like_url(url):
            issues.append(Issue("FAIL", item.title, "invalid_url_format", f"URL 格式明显非法：{url}"))
            continue
        if url in seen:
            issues.append(Issue("WARN", item.title, "duplicate_url", f"链接与《{seen[url]}》重复：{url}"))
        else:
            seen[url] = item.title

        link_check = check_link_status(url)
        link_checks[url] = link_check
        if link_check.error:
            issues.append(Issue("WARN", item.title, "link_check_error", link_check.error))
            continue

        status = link_check.status_code
        if status in INVALID_STATUS_CODES and declared_as_accessible(item.declared_link_status):
            issues.append(
                Issue(
                    "FAIL",
                    item.title,
                    "declared_accessible_but_bad_status",
                    f"日报声明链接可访问，但实际状态码为 {status}。",
                )
            )
        if status is not None and 200 <= status < 300 and declared_as_inaccessible(item.declared_link_status):
            issues.append(
                Issue(
                    "WARN",
                    item.title,
                    "declared_inaccessible_but_ok",
                    f"日报声明链接不可访问/不足，但实际状态码为 {status}。",
                )
            )
        if link_check.final_url and redirect_domain_changed(url, link_check.final_url):
            issues.append(
                Issue(
                    "WARN",
                    item.title,
                    "redirect",
                    f"链接发生重定向：{url} -> {link_check.final_url}",
                )
            )

    return issues, link_checks


def normalize_url(url: str) -> str:
    return url.rstrip("/")


def normalize_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.")


def redirect_domain_changed(original_url: str, final_url: str) -> bool:
    original_domain = normalize_domain(original_url)
    final_domain = normalize_domain(final_url)
    return bool(original_domain and final_domain and original_domain != final_domain)


def text_is_specific(text: str) -> bool:
    if len(text.strip()) >= 120:
        return True
    markers = ("参数", "模型", "架构", "CI/CD", "GPU", "RAG", "Agent", "基准", "许可证", "训练", "部署")
    return sum(1 for marker in markers if marker.lower() in text.lower()) >= 3


def get_field_text(item: NewsItem, field_name: str) -> str:
    value = item.fields.get(field_name, "")
    if value:
        return value
    # Fall back to a loose section search for multi-line fields.
    pattern = rf"{re.escape(field_name)}\*\*?\s*[:：](.*?)(?:\n-\s*\*\*|\n-\s*[\u4e00-\u9fffA-Za-z ]+[:：]|\Z)"
    match = re.search(pattern, item.raw, flags=re.DOTALL)
    return match.group(1).strip() if match else ""


def check_overclaiming(items: list[NewsItem], link_checks: dict[str, LinkCheck]) -> list[Issue]:
    issues: list[Issue] = []
    for item in items:
        link_check = link_checks.get(item.url)
        link_bad = bool(link_check and link_check.status_code in INVALID_STATUS_CODES)
        content_weak = declared_as_inaccessible(item.declared_content_status)
        summary_text = " ".join(get_field_text(item, field) for field in SUMMARY_FIELDS)

        if link_bad and text_is_specific(summary_text):
            issues.append(
                Issue(
                    "WARN",
                    item.title,
                    "overclaiming_with_bad_link",
                    "链接不可访问，但条目仍包含较具体的技术总结。",
                )
            )
        if content_weak and text_is_specific(summary_text):
            issues.append(
                Issue(
                    "WARN",
                    item.title,
                    "overclaiming_with_weak_content",
                    "正文抓取状态显示失败/不足，但总结和行动建议较具体。",
                )
            )
    return issues


def run_quality_check(markdown_path: Path) -> QualityReport:
    markdown = load_markdown(markdown_path)
    items = parse_news_items(markdown)
    issues: list[Issue] = []

    low_value = is_low_value_report(markdown)
    if not items and not low_value:
        issues.append(Issue("FAIL", "日报整体", "no_news_items", "未解析到新闻条目，也不是低价值日报。"))

    issues.extend(check_low_value_report(markdown, items))
    issues.extend(check_required_fields(items))
    link_issues, link_checks = check_link_fields_match(items)
    issues.extend(link_issues)
    issues.extend(check_overclaiming(items, link_checks))

    return QualityReport(
        markdown_path=markdown_path,
        low_value_report=low_value,
        item_count=len(items),
        issues=issues,
        link_checks=link_checks,
    )


def write_quality_report(report: QualityReport, output_path: Path = QUALITY_REPORT_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Daily News Quality Report",
        "",
        f"- Markdown: `{report.markdown_path}`",
        f"- Low value report: `{report.low_value_report}`",
        f"- News items: `{report.item_count}`",
        f"- PASS items: `{report.pass_items}`",
        f"- WARN items: `{report.warn_items}`",
        f"- FAIL items: `{report.fail_items}`",
        f"- Overall WARN: `{report.overall_warn_count}`",
        f"- Overall FAIL: `{report.overall_fail_count}`",
        "",
        "## Issues",
        "",
    ]

    if not report.issues:
        lines.append("No issues found.")
    else:
        for issue in report.issues:
            lines.extend(
                [
                    f"### {issue.level}: {issue.issue_type}",
                    f"- Title: {issue.title}",
                    f"- Message: {issue.message}",
                    "",
                ]
            )

    lines.extend(["", "## Link Checks", ""])
    if not report.link_checks:
        lines.append("No links checked.")
    else:
        for check in report.link_checks.values():
            lines.extend(
                [
                    f"### {check.url}",
                    f"- Status code: {check.status_code}",
                    f"- Final URL: {check.final_url or 'N/A'}",
                    f"- Content-Type: {check.content_type or 'N/A'}",
                    f"- Error: {check.error or 'N/A'}",
                    "",
                ]
            )

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_path


def print_terminal_report(report: QualityReport, report_path: Path) -> None:
    print(f"Markdown: {report.markdown_path}")
    print(f"Low value report: {report.low_value_report}")
    print(f"Total news items: {report.item_count}")
    print(f"PASS items: {report.pass_items}  WARN items: {report.warn_items}  FAIL items: {report.fail_items}")
    print(f"Overall WARN: {report.overall_warn_count}  Overall FAIL: {report.overall_fail_count}")
    print(f"Quality report: {report_path}")
    if not report.issues:
        print("No issues found.")
        return

    print("\nIssues:")
    for issue in report.issues:
        print(f"- [{issue.level}] {issue.title} | {issue.issue_type}: {issue.message}")


def main() -> None:
    markdown_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MARKDOWN_PATH
    if not markdown_path.is_absolute():
        markdown_path = ROOT_DIR / markdown_path

    try:
        report = run_quality_check(markdown_path)
        report_path = write_quality_report(report)
        print_terminal_report(report, report_path)
    except Exception as exc:
        print(f"Quality check failed: {exc}")
        raise


if __name__ == "__main__":
    main()
