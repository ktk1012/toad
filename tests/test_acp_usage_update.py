import unittest
from unittest.mock import patch

from toad.acp import messages
from toad.acp.agent import Agent


class AgentUsageUpdateTest(unittest.TestCase):
    def test_formats_usage_without_cost(self) -> None:
        status = Agent._format_usage_update_status_line(
            {"sessionUpdate": "usage_update", "used": 12_345, "size": 200_000}
        )

        self.assertEqual(status, "Context 12.3k / 200k (6%)")

    def test_formats_usage_with_cost(self) -> None:
        status = Agent._format_usage_update_status_line(
            {
                "sessionUpdate": "usage_update",
                "used": 12_345,
                "size": 200_000,
                "cost": {"amount": 0.04, "currency": "USD"},
            }
        )

        self.assertEqual(status, "Context 12.3k / 200k (6%) · $0.04")

    def test_posts_status_line_for_usage_update(self) -> None:
        agent = Agent.__new__(Agent)

        with patch.object(agent, "post_message", return_value=True) as post_message:
            agent.rpc_session_update(
                "session",
                {
                    "sessionUpdate": "usage_update",
                    "used": 12_345,
                    "size": 200_000,
                },
            )

        post_message.assert_called_once()
        message = post_message.call_args.args[0]
        self.assertIsInstance(message, messages.UpdateStatusLine)
        self.assertEqual(message.status_line, "Context 12.3k / 200k (6%)")
