# channels

The ways messages reach a room and its answers come back: Discord, and the live page, a room you open as a browser tab.

- `__init__.py`: marks the folder as a Python package.
- `discord.py`: the Discord connection: reads the channels named in rooms.toml, posts each room's progress card, reply, files and voice notes, and answers the slash commands.
- `page.html`: the live page in your browser, one file with its own style and script: the program and tests editors, the test results, a score bar with a countdown, a layer to draw on, and the chat.
- `page.py`: serves the live page from yerkd, runs its tests in a sandbox with no network, and carries what you type, draw and say there to the room as your messages; it also opens the page, or a file a room shows you, on your screen.
