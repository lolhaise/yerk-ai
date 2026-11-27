# hello

The example add-on: one MCP tool that greets the room, in the shape an add-on in a repository of its own has. `yerk add` installs it from this folder; docs/GUIDE.md, Add-ons, says how add-ons work.

- `addon.toml`: its manifest: its entry for abilities.toml and what `yerk add` installs (its server, tool, setting, unit, budget line, words file and tests).
- `pyproject.toml`: its Python project file, for the package `yerk-hello`.
- `tests/`: `test_hello.py`, its fast test: the tool greets the room by the name the room's settings give.
- `units/`: `yerk-hello.service`, a unit that prints the greeting once, to show where an add-on's units go.
- `yerk_hello/`: its code: `__main__.py` is the MCP server with the hello tool, `words.py` holds every line it says, and `__init__.py` makes it a package.
