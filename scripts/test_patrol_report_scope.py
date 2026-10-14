# -*- coding: utf-8 -*-
"""Regression tests for patrol-report scope selection and direct export."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_service.core import orchestrator
from agent_service.report import feishu_card as fc
from agent_service.report import patrol_pdf
from shared import repo
import shared.db as shared_db
from shared.scene import point


SUMMARY = {
    "day": "2026-10-14",
    "runs": 1,
    "points": 2,
    "abnormal": 1,
    "normal": 1,
    "unfinished": 0,
    "image_missing": 0,
    "has_mock": False,
}


class PatrolReportScopeTests(unittest.TestCase):
    def test_period_bounds_use_natural_week_and_month(self) -> None:
        self.assertEqual(
            patrol_pdf._period_bounds("week", "2026-10-14")[:2],
            ("2026-10-12", "2026-10-18"),
        )
        self.assertEqual(
            patrol_pdf._period_bounds("month", "2026-10-14")[:2],
            ("2026-10-01", "2026-10-31"),
        )

    def test_available_periods_group_deduplicate_and_sort(self) -> None:
        today = date.today()
        previous_week = today - timedelta(days=today.weekday() + 7)
        previous_month = today.replace(day=1) - timedelta(days=1)
        runs = [
            {"id": "today-a", "started_at": f"{today.isoformat()}T09:00:00"},
            {"id": "today-b", "started_at": f"{today.isoformat()}T10:00:00"},
            {"id": "last-week", "started_at": f"{previous_week.isoformat()}T10:00:00"},
            {"id": "last-month", "started_at": f"{previous_month.isoformat()}T10:00:00"},
        ]
        with patch.object(patrol_pdf.repo, "latest_patrol_runs", return_value=runs):
            days = patrol_pdf.available_report_periods("day")
            weeks = patrol_pdf.available_report_periods("week")
            months = patrol_pdf.available_report_periods("month")

        unique_days = sorted({today, previous_week, previous_month}, reverse=True)
        unique_weeks = {
            day - timedelta(days=day.weekday()) for day in unique_days
        }
        unique_months = {(day.year, day.month) for day in unique_days}
        self.assertEqual(
            [period["start_day"] for period in days],
            [day.isoformat() for day in unique_days],
        )
        self.assertEqual(len(weeks), len(unique_weeks))
        self.assertEqual(len(months), len(unique_months))

    def test_abnormal_uses_annotated_image_and_normal_uses_raw_image(self) -> None:
        with TemporaryDirectory() as temp:
            annotated = Path(temp) / "annotated.jpg"
            raw = Path(temp) / "raw.jpg"
            annotated.write_bytes(b"annotated")
            raw.write_bytes(b"raw")
            base_capture = {
                "detections": [{"label": "person", "confidence": 0.91}],
                "image_path": str(annotated),
                "raw_image_path": str(raw),
                "infer_ms": 20,
                "is_mock": False,
            }
            abnormal = patrol_pdf._point_report(
                point("P1"), "RUN-test", {**base_capture, "result_status": "abnormal"}, []
            )
            normal = patrol_pdf._point_report(
                point("P1"), "RUN-test", {**base_capture, "result_status": "normal"}, []
            )

        self.assertEqual(abnormal.image_path, annotated)
        self.assertIn("标注帧", abnormal.image_label)
        self.assertEqual(normal.image_path, raw)
        self.assertIn("原始帧", normal.image_label)

    def test_point_without_capture_or_evidence_is_unfinished(self) -> None:
        with TemporaryDirectory() as temp, patch.object(patrol_pdf, "IMAGE_DIR", Path(temp)):
            report = patrol_pdf._point_report(point("P1"), "RUN-missing", None, [])

        self.assertEqual(report.status, "not_visited")
        self.assertIsNone(report.image_path)

    def test_unspecified_scope_returns_choice_card(self) -> None:
        replies = orchestrator._run_tool(
            "export_patrol_report", {}, "test-chat", "test-user", "导出巡逻报告"
        )

        self.assertEqual(replies[0].kind, "card")
        actions = replies[0].card["elements"][1]["actions"]
        self.assertEqual(
            {action["value"]["scope"] for action in actions},
            {"latest", "day", "week", "month", "task"},
        )

    def test_explicit_date_exports_directly(self) -> None:
        path = Path("report.pdf")
        with patch.object(
            orchestrator.T, "build_patrol_pdf", return_value=(path, SUMMARY)
        ) as build:
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "day", "date": "2026-10-14"},
                "test-chat",
                "test-user",
                "导出 2026-10-14 的巡逻报告",
            )

        build.assert_called_once_with(day="2026-10-14", scope="day")
        self.assertEqual(replies[0].kind, "file")
        self.assertEqual(replies[0].file_path, str(path))

    def test_explicit_week_and_month_export_directly(self) -> None:
        for scope, request in (
            ("week", "导出本周巡逻报告"),
            ("month", "导出本月巡逻报告"),
        ):
            with self.subTest(scope=scope):
                with patch.object(
                    orchestrator.T, "build_patrol_pdf", return_value=(Path("period.pdf"), SUMMARY)
                ) as build:
                    replies = orchestrator._run_tool(
                        "export_patrol_report",
                        {"scope": scope, "date": "2026-10-14"},
                        "test-chat",
                        "test-user",
                        request,
                    )

                build.assert_called_once_with(day="2026-10-14", scope=scope)
                self.assertEqual(replies[0].kind, "file")

    def test_explicit_task_exports_directly(self) -> None:
        path = Path("task-report.pdf")
        with patch.object(
            orchestrator.T, "build_patrol_pdf", return_value=(path, SUMMARY)
        ) as build:
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "task", "task_id": "T-20261014-001"},
                "test-chat",
                "test-user",
                "导出任务 T-20261014-001 的报告",
            )

        build.assert_called_once_with(task_id="T-20261014-001", created_by="test-user")
        self.assertEqual(replies[0].kind, "file")

    def test_week_without_anchor_returns_period_choices(self) -> None:
        periods = [{"label": "本周 10-12 至 10-18", "start_day": "2026-10-12", "end_day": "2026-10-18"}]
        with patch.object(orchestrator.T, "available_report_periods", return_value=periods):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "week"},
                "test-chat",
                "test-user",
                "按周导出巡逻报告",
            )

        self.assertEqual(replies[0].kind, "card")
        self.assertEqual(replies[0].card["header"]["title"]["content"], "📄 选择周")

    def test_empty_period_picker_shows_back_button(self) -> None:
        with patch.object(orchestrator.T, "available_report_periods", return_value=[]):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "day"},
                "test-chat",
                "test-user",
                "按天导出巡逻报告",
            )

        card = replies[0].card
        self.assertIn("当前没有可选范围", card["elements"][1]["text"]["content"])
        self.assertEqual(card["elements"][-1]["actions"][0]["value"]["scope"], "menu")

    def test_invalid_date_returns_validation_card(self) -> None:
        with patch.object(
            orchestrator.T, "build_patrol_pdf", side_effect=ValueError("bad date")
        ):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "day", "date": "not-a-date"},
                "test-chat",
                "test-user",
                "导出错误日期的报告",
            )

        self.assertEqual(replies[0].kind, "card")
        self.assertEqual(replies[0].card["header"]["title"]["content"], "⚠️ 日期格式不正确")

    def test_unknown_task_and_run_produce_empty_result(self) -> None:
        with (
            patch.object(orchestrator.T, "build_patrol_pdf", return_value=(None, SUMMARY)) as build,
        ):
            task_replies = orchestrator._run_tool(
                "export_patrol_report",
                {"task_id": "T-missing"},
                "test-chat",
                "test-user",
                "导出任务 T-missing 的报告",
            )
            run_replies = orchestrator.handle_card_action(
                "test-chat", "test-user", {"act": fc.ACT_REPORT_RUN, "run_id": "RUN-missing"}
            )

        self.assertEqual(build.call_count, 2)
        self.assertIn("没有巡逻记录", task_replies[0].card["header"]["title"]["content"])
        self.assertIn("没有巡逻记录", run_replies.replies[0].card["header"]["title"]["content"])

    def test_period_callback_validation_errors_return_error_card(self) -> None:
        with patch.object(
            orchestrator.T, "build_patrol_pdf", side_effect=ValueError("invalid period")
        ):
            result = orchestrator.handle_card_action(
                "test-chat",
                "test-user",
                {
                    "act": fc.ACT_REPORT_PERIOD,
                    "start_day": "bad",
                    "end_day": "2026-10-14",
                    "label": "bad range",
                },
            )

        self.assertEqual(result.response_card["header"]["title"]["content"], "⚠️ 操作未完成")

    def test_missing_task_or_run_is_handled_without_database_write(self) -> None:
        with (
            patch.object(patrol_pdf.repo, "run_for_task", return_value=None),
            patch.object(patrol_pdf.repo, "get_patrol_run_detail", return_value=None),
        ):
            task_path, task_summary = patrol_pdf.build_patrol_pdf(
                task_id="T-missing", created_by="test-user"
            )
            run_path, run_summary = patrol_pdf.build_patrol_pdf(
                run_id="RUN-missing", created_by="test-user"
            )

        self.assertIsNone(task_path)
        self.assertIsNone(run_path)
        self.assertEqual(task_summary["runs"], 0)
        self.assertEqual(run_summary["runs"], 0)

    def test_single_run_export_requires_requester_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "需要发起人身份"):
            patrol_pdf.build_patrol_pdf(task_id="T-20261014-001")

    def test_task_and_run_lookup_enforces_creator(self) -> None:
        with TemporaryDirectory() as temp, patch.object(
            shared_db, "DB_PATH", Path(temp) / "patrol.sqlite"
        ), patch.object(patrol_pdf, "EXPORT_DIR", Path(temp) / "exports"), patch.object(
            patrol_pdf, "_render_pdf"
        ) as render:
            shared_db.init_db()
            with shared_db.connect() as conn:
                conn.execute(
                    """INSERT INTO task
                       (id, robot_id, route_id, status, created_by, created_at, source_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("T-owner", "R-01", "standard_patrol", "completed", "user-a",
                     "2026-10-14T10:00:00", "test"),
                )
                conn.execute(
                    """INSERT INTO patrol_run
                       (id, task_id, robot_id, started_at, status, points_total, points_done)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("RUN-owner", "T-owner", "R-01", "2026-10-14T10:00:00",
                     "completed", 2, 2),
                )

            self.assertIsNotNone(repo.run_for_task("T-owner", created_by="user-a"))
            self.assertIsNone(repo.run_for_task("T-owner", created_by="user-b"))
            self.assertIsNotNone(
                repo.get_patrol_run_detail("RUN-owner", created_by="user-a")
            )
            self.assertIsNone(
                repo.get_patrol_run_detail("RUN-owner", created_by="user-b")
            )
            owned_path, owned_summary = patrol_pdf.build_patrol_pdf(
                task_id="T-owner", created_by="user-a"
            )
            other_path, other_summary = patrol_pdf.build_patrol_pdf(
                task_id="T-owner", created_by="user-b"
            )

        self.assertIsNotNone(owned_path)
        self.assertEqual(owned_summary["runs"], 1)
        self.assertIsNone(other_path)
        self.assertEqual(other_summary["runs"], 0)
        render.assert_called_once()

    def test_real_pdf_entry_rejects_invalid_and_reversed_dates(self) -> None:
        with self.assertRaises(ValueError):
            patrol_pdf.build_patrol_pdf(day="not-a-date")
        with self.assertRaisesRegex(ValueError, "结束日期不能早于开始日期"):
            patrol_pdf.build_patrol_pdf(start_day="2026-10-15", end_day="2026-10-14")

    def test_pdf_build_failure_is_returned_as_error_card(self) -> None:
        with patch.object(
            orchestrator.T, "build_patrol_pdf", side_effect=RuntimeError("pdf failure")
        ):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "day", "date": "2026-10-14"},
                "test-chat",
                "test-user",
                "导出今天的报告",
            )

        self.assertEqual(replies[0].kind, "card")
        self.assertEqual(replies[0].card["header"]["title"]["content"], "⚠️ PDF 报告生成失败")

    def test_task_scope_opens_task_picker_and_selection_exports_one_run(self) -> None:
        run = {
            "id": "RUN-20261014-001",
            "task_id": "T-20261014-001",
            "started_at": "2026-10-14T10:00:00",
            "route_name": "标准巡检路线",
            "status": "completed",
        }
        with patch.object(orchestrator.T, "latest_report_runs", return_value=[run]):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "task"},
                "test-chat",
                "test-user",
                "选择某个任务导出报告",
            )

        self.assertEqual(replies[0].kind, "card")
        task_button = next(
            element["actions"][0]
            for element in replies[0].card["elements"]
            if element.get("tag") == "action"
            and element["actions"][0]["value"].get("act") == fc.ACT_REPORT_RUN
        )
        self.assertEqual(task_button["value"]["run_id"], run["id"])

        path = Path("selected-task.pdf")
        with patch.object(
            orchestrator.T, "build_patrol_pdf", return_value=(path, SUMMARY)
        ) as build:
            result = orchestrator.handle_card_action(
                "test-chat", "test-user", task_button["value"]
            )

        build.assert_called_once_with(run_id=run["id"], created_by="test-user")
        self.assertEqual(result.replies[0].kind, "file")

    def test_latest_scope_exports_single_latest_run(self) -> None:
        path = Path("latest-run.pdf")
        with (
            patch.object(orchestrator.T, "latest_report_runs", return_value=[{"id": "RUN-latest"}]),
            patch.object(orchestrator.T, "build_patrol_pdf", return_value=(path, SUMMARY)) as build,
        ):
            replies = orchestrator._run_tool(
                "export_patrol_report",
                {"scope": "latest"},
                "test-chat",
                "test-user",
                "导出最近一次巡逻报告",
            )

        build.assert_called_once_with(run_id="RUN-latest", created_by="test-user")
        self.assertEqual(replies[0].file_path, str(path))

    def test_period_card_action_generates_selected_range(self) -> None:
        path = Path("week-report.pdf")
        with patch.object(
            orchestrator.T, "build_patrol_pdf", return_value=(path, SUMMARY)
        ) as build:
            result = orchestrator.handle_card_action(
                "test-chat",
                "test-user",
                {
                    "act": fc.ACT_REPORT_PERIOD,
                    "start_day": "2026-10-12",
                    "end_day": "2026-10-18",
                    "label": "本周 10-12 至 10-18",
                },
            )

        build.assert_called_once_with(
            start_day="2026-10-12",
            end_day="2026-10-18",
            period_label="本周 10-12 至 10-18",
        )
        self.assertEqual(result.replies[0].kind, "file")


if __name__ == "__main__":
    unittest.main()