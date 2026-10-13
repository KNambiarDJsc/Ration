# Security Policy

RATION runs inside your coding-agent workflow (Claude Code hooks and status line), so security and privacy issues are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. Include steps to reproduce, the affected version (`ration --version`), and your OS / Claude Code version. You should get a response within a few days.

## What counts as a security issue

- Anything that sends local data off the machine, or could be made to.
- Storing prompt text, source code, diffs, full commands, tool output or credentials (RATION promises not to).
- An autopilot run writing to your checkout, pushing, merging, or accepting a change that its own policy forbids; repository content (a TODO, a README, a tracked `.ration/` file) causing a command to run or a rule to be dropped.
- A RATION hook blocking or breaking a Claude Code session, or being abusable to change agent behaviour beyond its documented, opt-in guard mode.
- Modifying Claude Code / Codex settings beyond the documented hook, status line and backup behaviour (for example permissions, auth or sandbox settings).
- Command injection via configuration, acceptance checks or chained status line commands that goes beyond what the user configured.

## Design guarantees

Autopilot's safety model and its trust boundaries (which commands run, what repository content can and cannot influence) are in `docs/autopilot-architecture.md`. In short: check commands are yours and run your project's code; repository text never becomes a command or an instruction; an autonomous run never pushes, merges or deploys.

- No network code and no telemetry.
- Hooks fail open; internal errors are written to `~/.config/ration/hook-errors.log`.
- `ration init` backs up `~/.claude/settings.json` before writing and refuses to touch it if it isn't valid JSON.
- RATION never passes `bypassPermissions` / `--dangerously-*` flags and never reads credential files.
