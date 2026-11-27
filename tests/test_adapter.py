"""Acceptance row 8: the adapter's tools are the agent's operations, correctly annotated, and the
plugin package declares `trust: untrusted` for its server."""

import json
import re
import sys
import unittest
from pathlib import Path

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from hermes_gmail.client import GateClient
from hermes_gmail.mcp_adapter import build_server
from tests.harness import Reply
from tests.test_service import AGENT, ServiceTestCase

ROOT = Path(__file__).resolve().parent.parent


class Adapter(ServiceTestCase):
    def adapter(self):
        return build_server(GateClient(self.socket_path, caller="agent", timeout=10))

    def test_tools_are_the_agents_operations_and_reads_are_marked_read_only(self):
        self.start(AGENT)

        async def check():
            async with Client(self.adapter()) as client:
                return (await client.list_tools()).tools

        tools = anyio.run(check)
        names = {tool.name for tool in tools}
        self.assertEqual(names, set(self.dispatcher.operations_for(AGENT)) - self.write_operations())
        for tool in tools:
            with self.subTest(tool.name):
                self.assertTrue(tool.annotations and tool.annotations.read_only_hint)

    def write_operations(self):
        """Operations an adapter tool must offer without a read-only hint (slice 5)."""
        return set()

    def test_a_call_goes_through_the_socket_and_errors_keep_their_category(self):
        self.start(AGENT)
        self.google.gmail = lambda seen: Reply(200, {"messages": [{"id": "a"}]})

        async def calls():
            async with Client(self.adapter()) as client:
                ok = await client.call_tool("search", {"query": "in:inbox"})
                bad = await client.call_tool("search", {"query": "q", "page_bound": 0})
                return ok, bad

        ok, bad = anyio.run(calls)
        self.assertFalse(ok.is_error)
        self.assertEqual(ok.structured_content["ids"], ["a"])
        self.assertTrue(bad.is_error)
        # The SDK prefixes "Error executing tool search: "; the category follows it.
        self.assertIn(": refused: page_bound", bad.content[0].text)

    def test_the_entry_point_serves_mcp_on_stdio(self):
        self.start(AGENT)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "hermes_gmail.cli", "mcp"],
            env={"HERMES_GMAIL_SOCKET": str(self.socket_path), "HERMES_GMAIL_CALLER": "agent"},
        )

        async def check():
            async with Client(params) as client:
                return {tool.name for tool in (await client.list_tools()).tools}

        self.assertIn("read_thread", anyio.run(check))


class PluginPackage(unittest.TestCase):
    """The shape Hermes' portable-package loader checks (hermes-agent@28ef6e4:
    hermes_cli/agent_plugins.py:230-260, 414-449, 460-475)."""

    def test_manifest_declares_the_server_untrusted(self):
        manifest = json.loads((ROOT / "plugin.json").read_text())
        servers = json.loads((ROOT / "mcp.json").read_text())
        self.assertEqual(manifest["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertRegex(manifest["name"], r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
        declared = manifest["extensions"]["com.nousresearch.hermes"]["servers"]
        self.assertEqual(set(declared), set(servers["mcpServers"]))
        self.assertTrue(all(entry == {"trust": "untrusted"} for entry in declared.values()))

    def test_mcp_entry_is_a_bare_stdio_command(self):
        servers = json.loads((ROOT / "mcp.json").read_text())
        self.assertEqual(set(servers), {"$schema", "mcpServers"})
        self.assertEqual(servers["$schema"], "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        for entry in servers["mcpServers"].values():
            self.assertEqual(entry["type"], "stdio")
            self.assertNotRegex(entry["command"], r"[/\\\s]")
            self.assertTrue(set(entry) <= {"type", "command", "args", "env", "cwd"})

    def test_skills_have_matching_names(self):
        for skill in (ROOT / "skills").iterdir():
            front = (skill / "SKILL.md").read_text().split("---")[1]
            self.assertIn(f"name: {skill.name}\n", front)
            self.assertRegex(skill.name, r"^[a-z0-9-]{1,64}$")
            description = re.search(r"^description: (.+)$", front, re.MULTILINE).group(1)
            self.assertLessEqual(len(description), 1024)


if __name__ == "__main__":
    unittest.main()
