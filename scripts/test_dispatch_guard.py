# -*- coding: utf-8 -*-
"""Regression tests for the LLM task-dispatch guard."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_service.core import orchestrator


class PatrolDispatchGuardTests(unittest.TestCase):
    def test_report_and_help_requests_cannot_prepare_patrol(self) -> None:
        messages = (
            "下载本次任务的报告",
            "获取最新一次的报表",
            "获取报表",
            "你都有什么功能",
            "可不可以去巡逻",
            "要不要让机器人去巡逻",
            "不能让机器人去巡逻",
            "不可以让机器人去巡逻",
            "我建议让机器人去巡逻一圈",
            "领导说让机器人去巡逻一圈",
            "“让机器人去巡逻一圈”",
            "“让机器人去巡逻一圈”是什么意思？",
            "如果让机器人去巡逻会怎样",
            "假设让机器人去巡逻一圈",
            "不要让机器人去巡逻",
            "我不需要开始巡逻",
        )

        for message in messages:
            with self.subTest(message=message):
                with patch.object(orchestrator.T, "prepare_patrol_task") as prepare:
                    replies = orchestrator._run_tool(
                        "prepare_patrol_task", {}, "test-chat", "test-user", message
                    )

                prepare.assert_not_called()
                self.assertTrue(any("没有创建任务" in (reply.text or "") for reply in replies))

    def test_explicit_patrol_commands_still_prepare_confirmation(self) -> None:
        for message in ("让机器人去巡逻一圈", "进行巡逻"):
            with self.subTest(message=message):
                with (
                    patch.object(
                        orchestrator.T, "prepare_patrol_task", return_value=("PA-test", [])
                    ) as prepare,
                    patch.object(orchestrator.memory, "remember_pending"),
                ):
                    replies = orchestrator._run_tool(
                        "prepare_patrol_task", {}, "test-chat", "test-user", message
                    )

                prepare.assert_called_once()
                self.assertEqual([reply.kind for reply in replies], ["card"])


if __name__ == "__main__":
    unittest.main()