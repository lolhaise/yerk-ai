# Security

Yerk AI is a personal tool with one maintainer. It runs on your own machine, as your user, with your own Claude Code login. README.md, "Who it trusts", says what it defends against and what it does not.

## Reporting a problem

Report a security problem privately, through GitHub's private vulnerability reporting on this repository: the Security tab, then "Report a vulnerability". Do not open a public issue for it.

Say what you found, the commit it is on and how to reproduce it. There is one maintainer, so an answer can take a few days.

## What it covers

The code in this repository: the daemon (`yerkd`), the `yerk` command, the hooks, the MCP server, the units in `units/` and the web plug-in in `plugins/web`. Claude Code, Discord and the services an add-on calls are not; report those to their makers.
