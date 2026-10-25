"""`python -m yerk_hello --private P --room R`: the hello MCP server over stdio, one tool."""
import argparse
import tomllib
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import words


def greeting(private: Path, room: str) -> str:
    """[hello] greeting in yerkd.toml to the room's hello_name in rooms.toml, else the words file's."""
    def read(name: str) -> dict:
        return tomllib.loads((private / name).read_text()) if (private / name).is_file() else {}
    who = read("rooms.toml").get("rooms", {}).get(room, {}).get("hello_name") or words.SOMEONE
    return words.GREETING.format(greeting=read("yerkd.toml").get("hello", {}).get("greeting") or words.HELLO, name=who)


def server(private: Path, room: str) -> FastMCP:
    mcp = FastMCP("hello")
    mcp.tool(name="hello", description="Greets the room by the name its rooms.toml table gives.")(lambda: greeting(private, room))
    return mcp


def main() -> None:
    p = argparse.ArgumentParser(prog="yerk_hello")
    p.add_argument("--private", required=True)
    p.add_argument("--room", required=True)
    a = p.parse_args()
    server(Path(a.private), a.room).run()


if __name__ == "__main__":
    main()
