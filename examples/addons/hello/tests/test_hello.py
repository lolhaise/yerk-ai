"""The hello add-on's fast test: its server holds the one tool, whose line comes from the room's table, the yerkd.toml
table and the words file."""
import asyncio

from yerk_hello.__main__ import server


def test_the_one_tool_greets_by_the_rooms_name_and_its_yerkd_table(tmp_path):
    def said():
        return asyncio.run(server(tmp_path, "r").call_tool("hello", {}))[0].text
    assert [t.name for t in asyncio.run(server(tmp_path, "r").list_tools())] == ["hello"]
    assert said() == "Hello, there, from the hello add-on."
    (tmp_path / "rooms.toml").write_text('[rooms.r]\nhello_name = "Nora"\n')
    (tmp_path / "yerkd.toml").write_text('[hello]\ngreeting = "Hi"\n')
    assert said() == "Hi, Nora, from the hello add-on."
