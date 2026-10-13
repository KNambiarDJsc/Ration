---
name: goal
description: Show or set this project's goal, priorities and constraints, which RATION Autopilot uses to rank work. Stored locally in .ration/goal.toml.
argument-hint: "[the goal, in a sentence]"
disable-model-invocation: true
allowed-tools: Bash(ration goal:*)
---

- With no arguments: run `ration goal show` and show the output.
- With arguments: the text is the goal. Ask the user for priorities (most important first) and for constraints, meaning things
  autopilot must not do, such as "don't change the database schema". Then run
  `ration goal set "<goal>" --priority "<p1>" --priority "<p2>" --constraint "<c1>"` with what they gave you, and show the result.

Record only what the user said. Do not invent priorities or constraints.
