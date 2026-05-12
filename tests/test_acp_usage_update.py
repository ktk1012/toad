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

    def test_posts_status_line_from_context_command_output(self) -> None:
        agent = Agent.__new__(Agent)
        context_output = """Context Usage

Model: bifrost/gpt-5.5[1m]
Tokens: 25.9k / 1m (3%)
"""

        with patch.object(agent, "post_message", return_value=True) as post_message:
            agent.rpc_session_update(
                "session",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": context_output},
                },
            )

        status_lines = [
            call.args[0].status_line
            for call in post_message.call_args_list
            if isinstance(call.args[0], messages.UpdateStatusLine)
        ]
        self.assertEqual(status_lines, ["Context 25.9k / 1m (3%)"])

    def test_context_command_output_overrides_stale_default_window(self) -> None:
        agent = Agent.__new__(Agent)
        context_output = """Context Usage

Model: bifrost/gpt-5.5[1m]
Tokens: 25.9k / 1m (3%)
"""

        with patch.object(agent, "post_message", return_value=True) as post_message:
            agent.rpc_session_update(
                "session",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": context_output},
                },
            )
            agent.rpc_session_update(
                "session",
                {
                    "sessionUpdate": "usage_update",
                    "used": 0,
                    "size": 200_000,
                    "cost": {"amount": 0.06, "currency": "USD"},
                },
            )

        status_lines = [
            call.args[0].status_line
            for call in post_message.call_args_list
            if isinstance(call.args[0], messages.UpdateStatusLine)
        ]
        self.assertEqual(
            status_lines,
            ["Context 25.9k / 1m (3%)", "Context 25.9k / 1m (3%) · $0.06"],
        )
