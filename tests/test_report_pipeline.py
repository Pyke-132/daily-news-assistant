import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import main


def candidate(title="Item One", url="https://example.com/news?utm_source=x#top", score=42):
    return main.NewsItem(
        title=title,
        source="Example Source",
        url=url,
        summary="RSS summary",
        body="Article body " * 80,
        score=score,
        matched_keywords=["RAG", "Agent"],
        feedback_score=0,
        adjusted_score=score,
        feedback_reasons=[],
        url_status=200,
        content_length=960,
        content_available=True,
        invalid_link=False,
        weak_content=False,
        suggested_search_query=title,
        published="2026-06-12",
    )


def item_dict(title="Item One", url="https://example.com/news?utm_source=x#top", score=8.5):
    return {
        "title": title,
        "source": "Example Source",
        "url": url,
        "published": "2026-06-12",
        "relevance_score": score,
        "link_status": "200",
        "content_status": "content_available",
        "front_context": "Background",
        "why_recommend": "Useful for the user's work.",
        "why_not_recommend": "Limited implementation details.",
        "one_sentence": "One sentence.",
        "original_facts": ["Fact from source."],
        "model_summary": ["Model summary."],
        "related_thoughts": ["Related thought."],
        "action": "Read it.",
        "matched_keywords": ["RAG"],
    }


def formal_report_dict(items=None):
    return {
        "schema_version": main.SCHEMA_VERSION,
        "report_type": "run",
        "report_date": "2026-06-12",
        "generated_at": "2026-06-12T12:00:00",
        "status": "formal",
        "overview": "本次最值得关注的是开发者模型和 Agent 风险。两者都指向轻量工具越来越强，但安全边界仍需要认真处理。",
        "candidate_count": 1,
        "items": items if items is not None else [item_dict()],
        "optional_references": [],
        "low_value_reason": "",
        "error_stage": "",
        "error_message": "",
    }


class ReportPipelineTests(unittest.TestCase):
    def test_parse_llm_json_accepts_plain_json_and_code_fence(self):
        data = formal_report_dict()
        plain = main.parse_llm_json(json.dumps(data, ensure_ascii=False))
        fenced = main.parse_llm_json("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")

        self.assertEqual(plain["status"], "formal")
        self.assertEqual(fenced["items"][0]["title"], "Item One")

    def test_parse_llm_json_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            main.parse_llm_json("{not json")

    def test_validate_formal_report_accepts_candidate_items(self):
        report = main.validate_run_report(formal_report_dict(), [candidate()])

        self.assertEqual(report.status, "formal")
        self.assertIn("开发者模型", report.overview)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(report.items[0].relevance_score, 8.5)

    def test_validate_formal_report_requires_overview(self):
        data = formal_report_dict()
        data["overview"] = ""

        with self.assertRaises(ValueError):
            main.validate_run_report(data, [candidate()])

    def test_validate_low_value_report_requires_empty_items(self):
        data = formal_report_dict(items=[])
        data["status"] = "low_value"
        data["low_value_reason"] = "Nothing worth adding."
        report = main.validate_run_report(data, [candidate()])

        self.assertEqual(report.status, "low_value")
        self.assertEqual(report.items, [])

    def test_validate_rejects_invalid_status_and_empty_formal(self):
        invalid_status = formal_report_dict()
        invalid_status["status"] = "maybe"
        with self.assertRaises(ValueError):
            main.validate_run_report(invalid_status, [candidate()])

        empty_formal = formal_report_dict(items=[])
        with self.assertRaises(ValueError):
            main.validate_run_report(empty_formal, [candidate()])

    def test_validate_rejects_low_value_with_items_candidate_outside_and_bad_score(self):
        low_value = formal_report_dict()
        low_value["status"] = "low_value"
        low_value["low_value_reason"] = "Weak."
        with self.assertRaises(ValueError):
            main.validate_run_report(low_value, [candidate()])

        outside = formal_report_dict(items=[item_dict(title="Other", url="https://example.com/other")])
        with self.assertRaises(ValueError):
            main.validate_run_report(outside, [candidate()])

        bad_score = formal_report_dict(items=[item_dict(score=11)])
        with self.assertRaises(ValueError):
            main.validate_run_report(bad_score, [candidate()])

    def test_normalize_url_for_dedupe_keeps_identity_query_but_removes_tracking(self):
        left = "HTTPS://www.Example.com/path/?id=10&utm_source=x#frag"
        right = "https://example.com/path?id=10"
        different = "https://example.com/path?id=11"

        self.assertEqual(main.normalize_url_for_dedupe(left), main.normalize_url_for_dedupe(right))
        self.assertNotEqual(main.normalize_url_for_dedupe(right), main.normalize_url_for_dedupe(different))

    def test_merge_daily_items_keeps_old_duplicate_and_appends_new(self):
        old = main.report_item_from_dict(item_dict(title="Old", url="https://example.com/a?utm_source=x"))
        duplicate = main.report_item_from_dict(item_dict(title="New Duplicate", url="https://www.example.com/a/"))
        new = main.report_item_from_dict(item_dict(title="New", url="https://example.com/b"))

        merged = main.merge_daily_items([old], [duplicate, new])

        self.assertEqual([item.title for item in merged], ["Old", "New"])

    def test_daily_report_roundtrip_and_corrupt_daily_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            daily = main.DailyReport(
                schema_version=main.SCHEMA_VERSION,
                report_type="daily",
                report_date="2026-06-12",
                updated_at="2026-06-12T12:00:00",
                items=[main.report_item_from_dict(item_dict())],
            )
            main.write_json_atomic(path, main.daily_report_to_dict(daily))
            loaded = main.load_daily_report(path)

            self.assertEqual(len(loaded.items), 1)

            path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ValueError):
                main.load_daily_report(path)

    def test_daily_report_rejects_unsupported_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            data = main.daily_report_to_dict(
                main.DailyReport(
                    schema_version=main.SCHEMA_VERSION,
                    report_type="daily",
                    report_date="2026-06-12",
                    updated_at="2026-06-12T12:00:00",
                    items=[],
                )
            )
            data["schema_version"] = 999
            path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaises(ValueError):
                main.load_daily_report(path)

    def test_render_markdown_uses_report_state(self):
        formal = main.validate_run_report(formal_report_dict(), [candidate()])
        low_data = formal_report_dict(items=[])
        low_data["status"] = "low_value"
        low_data["low_value_reason"] = "No formal news."
        low = main.validate_run_report(low_data, [candidate()])
        failed = main.build_failed_report("json_parse", "bad json", "2026-06-12", "2026-06-12T12:00:00", 1)

        formal_markdown = main.render_run_markdown(formal)
        low_markdown = main.render_run_markdown(low)
        failed_markdown = main.render_run_markdown(failed)

        self.assertIn("运行状态：正式日报", formal_markdown)
        self.assertIn("## 本次概述", formal_markdown)
        self.assertIn(formal.overview, formal_markdown)
        self.assertIn("正文状态：正文可读", formal_markdown)
        self.assertIn("## 1. Item One", formal_markdown)
        self.assertIn("运行状态：今日无高价值新闻", low_markdown)
        self.assertIn("今日无高价值新闻", low_markdown)
        self.assertIn("运行状态：运行失败", failed_markdown)
        self.assertIn("本次运行失败", failed_markdown)

    def test_write_run_outputs_creates_matching_archive_stems_and_latest(self):
        report = main.validate_run_report(formal_report_dict(), [candidate()])
        with tempfile.TemporaryDirectory() as tmp:
            paths = main.write_run_outputs(Path(tmp), report)
            latest_json = json.loads(paths["latest_json"].read_text(encoding="utf-8"))

            self.assertTrue(paths["latest_json"].exists())
            self.assertTrue(paths["latest_md"].exists())
            self.assertEqual(latest_json["overview"], report.overview)
            self.assertEqual(paths["archive_json"].stem, paths["archive_md"].stem)

    def test_write_daily_outputs_only_for_formal_and_can_regenerate_markdown(self):
        report = main.validate_run_report(formal_report_dict(), [candidate()])
        with tempfile.TemporaryDirectory() as tmp:
            paths = main.write_daily_outputs(Path(tmp), report)
            daily = main.load_daily_report(paths["daily_json"])

            self.assertEqual(len(daily.items), 1)
            self.assertIn("Item One", paths["daily_md"].read_text(encoding="utf-8"))
            daily_markdown = main.render_daily_markdown(daily)
            self.assertIn("## 今日概览", daily_markdown)
            self.assertIn("累计正式新闻数：1", daily_markdown)
            self.assertIn("One sentence.", daily_markdown)
            self.assertIn("Item One", daily_markdown)

    def test_write_daily_outputs_does_not_create_daily_for_low_value_or_failed(self):
        low_data = formal_report_dict(items=[])
        low_data["status"] = "low_value"
        low_data["low_value_reason"] = "No formal news."
        low = main.validate_run_report(low_data, [candidate()])
        failed = main.build_failed_report("json_parse", "bad json", "2026-06-12", "2026-06-12T12:00:00", 1)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            self.assertEqual(main.write_daily_outputs(output_dir, low), {})
            self.assertEqual(main.write_daily_outputs(output_dir, failed), {})
            self.assertFalse((output_dir / "daily_news_2026-06-12.json").exists())
            self.assertFalse((output_dir / "daily_news_2026-06-12.md").exists())

    def test_write_daily_outputs_second_formal_appends_and_duplicate_keeps_old(self):
        first = main.validate_run_report(
            formal_report_dict(items=[item_dict(title="Old", url="https://example.com/a?utm_source=x")]),
            [candidate(title="Old", url="https://example.com/a?utm_source=x")],
        )
        second = main.validate_run_report(
            formal_report_dict(
                items=[
                    item_dict(title="Duplicate", url="https://www.example.com/a/"),
                    item_dict(title="New", url="https://example.com/b"),
                ]
            ),
            [
                candidate(title="Duplicate", url="https://www.example.com/a/"),
                candidate(title="New", url="https://example.com/b"),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            main.write_daily_outputs(output_dir, first)
            paths = main.write_daily_outputs(output_dir, second)
            daily = main.load_daily_report(paths["daily_json"])

            self.assertEqual([item.title for item in daily.items], ["Old", "New"])

    def test_daily_json_remains_when_daily_markdown_write_fails(self):
        report = main.validate_run_report(formal_report_dict(), [candidate()])
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            original_write_text_atomic = main.write_text_atomic

            def fail_markdown(path, text):
                if path.suffix == ".md":
                    raise OSError("markdown write failed")
                original_write_text_atomic(path, text)

            with mock.patch.object(main, "write_text_atomic", side_effect=fail_markdown):
                with self.assertRaises(OSError):
                    main.write_daily_outputs(output_dir, report)

            self.assertTrue((output_dir / "daily_news_2026-06-12.json").exists())
            self.assertFalse((output_dir / "daily_news_2026-06-12.md").exists())

    def test_atomic_json_write_failure_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            target.write_text('{"ok": true}\n', encoding="utf-8")

            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    main.write_json_atomic(target, {"ok": False})

            self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}\n')

    def test_select_processed_candidates_only_returns_formal_item_sources(self):
        selected = candidate()
        unselected = candidate(title="Other", url="https://example.com/other", score=7)
        report = main.validate_run_report(formal_report_dict(), [selected, unselected])

        result = main.select_processed_candidates(report, [selected, unselected])

        self.assertEqual(result, [selected])

    def test_isolated_three_run_end_to_end_accumulates_daily_from_json(self):
        candidate_a = candidate(title="News A", url="https://example.com/a", score=50)
        candidate_b = candidate(title="News B", url="https://example.com/b?utm_source=first", score=40)
        candidate_b_again = candidate(title="News B", url="https://www.example.com/b/?utm_source=second#section", score=40)
        candidate_c = candidate(title="News C", url="https://example.com/c", score=30)
        candidate_d = candidate(title="News D", url="https://example.com/d", score=20)

        first_data = formal_report_dict(
            items=[
                item_dict(title="News A", url="https://example.com/a", score=9),
                item_dict(title="News B", url="https://example.com/b?utm_source=first", score=8),
            ]
        )
        first_data["candidate_count"] = 2
        second_data = formal_report_dict(
            items=[
                item_dict(title="News B", url="https://www.example.com/b/?utm_source=second#section", score=8),
                item_dict(title="News C", url="https://example.com/c", score=7),
            ]
        )
        second_data["candidate_count"] = 2
        third_data = formal_report_dict(items=[])
        third_data["status"] = "low_value"
        third_data["overview"] = ""
        third_data["candidate_count"] = 1
        third_data["low_value_reason"] = "Only optional references were found."
        third_data["optional_references"] = [
            item_dict(title="News D", url="https://example.com/d", score=4)
        ]

        first_report = main.validate_run_report(first_data, [candidate_a, candidate_b])
        second_report = main.validate_run_report(second_data, [candidate_b_again, candidate_c])
        third_report = main.validate_run_report(third_data, [candidate_d])

        processed = []
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_times = [
                main.datetime(2026, 6, 12, 9, 0, 0),
                main.datetime(2026, 6, 12, 10, 0, 0),
                main.datetime(2026, 6, 12, 11, 0, 0),
            ]

            for report, candidates, run_time in [
                (first_report, [candidate_a, candidate_b], run_times[0]),
                (second_report, [candidate_b_again, candidate_c], run_times[1]),
                (third_report, [candidate_d], run_times[2]),
            ]:
                main.write_run_outputs(output_dir, report, run_time)
                if report.status == "formal":
                    daily_json_path = output_dir / f"daily_news_{report.report_date}.json"
                    existing_daily = main.load_daily_report(daily_json_path)
                    existing_items = existing_daily.items if existing_daily else []
                    main.write_daily_outputs(output_dir, report, run_time, existing_daily)
                    processed.extend(main.select_processed_candidates(report, candidates, existing_items))

            latest_json = json.loads((output_dir / "latest_daily_news.json").read_text(encoding="utf-8"))
            latest_md = (output_dir / "latest_daily_news.md").read_text(encoding="utf-8")
            daily_json_path = output_dir / "daily_news_2026-06-12.json"
            daily_md_path = output_dir / "daily_news_2026-06-12.md"
            daily = main.load_daily_report(daily_json_path)
            daily_md = daily_md_path.read_text(encoding="utf-8")
            archive_jsons = sorted((output_dir / "archive").glob("daily_news_2026-06-12_*.json"))
            archive_mds = sorted((output_dir / "archive").glob("daily_news_2026-06-12_*.md"))

            self.assertEqual(latest_json["status"], "low_value")
            self.assertEqual(latest_json["items"], [])
            self.assertEqual(latest_json["optional_references"][0]["title"], "News D")
            self.assertIn("今日无高价值新闻", latest_md)
            self.assertIn("News D", latest_md)
            self.assertNotIn("News D", daily_md)

            self.assertEqual(len(archive_jsons), 3)
            self.assertEqual(len(archive_mds), 3)
            self.assertEqual([path.stem for path in archive_jsons], [path.stem for path in archive_mds])
            for json_path, md_path in zip(archive_jsons, archive_mds):
                archive_data = json.loads(json_path.read_text(encoding="utf-8"))
                archive_md = md_path.read_text(encoding="utf-8")
                self.assertIn(main.display_status(archive_data["status"]), archive_md)

            self.assertEqual([item.title for item in daily.items], ["News A", "News B", "News C"])
            self.assertEqual(
                [main.normalize_url_for_dedupe(item.url) for item in daily.items],
                [
                    main.normalize_url_for_dedupe("https://example.com/a"),
                    main.normalize_url_for_dedupe("https://example.com/b?utm_source=first"),
                    main.normalize_url_for_dedupe("https://example.com/c"),
                ],
            )
            self.assertEqual(daily_md.count("## 1. News A"), 1)
            self.assertEqual(daily_md.count("## 2. News B"), 1)
            self.assertEqual(daily_md.count("## 3. News C"), 1)
            for item in daily.items:
                self.assertIn(item.title, daily_md)
                self.assertIn(item.url, daily_md)

            self.assertEqual([item.title for item in processed], ["News A", "News B", "News C"])


if __name__ == "__main__":
    unittest.main()
