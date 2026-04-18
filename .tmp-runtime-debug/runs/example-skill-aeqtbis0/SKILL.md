---
name: example-skill
summary: Generate a small markdown report inside an isolated workspace using a read-only template and optional notes.
version: 0.1.0
tags:
  - reporting
  - example
scripts:
  - run_report.py
references:
  - references/OPERATING_GUIDE.md
assets:
  - assets/report_template.md
---

# Example Skill

This skill demonstrates the progressive-disclosure contract expected by the MCP skill container:

1. Discovery: inspect the skill name and summary first.
2. Context loading: read this `SKILL.md` and the reference docs before deciding whether to execute anything.
3. Execution: run `scripts/run_report.py` inside an isolated workspace when a markdown report is actually needed.

## When To Use

Use this skill when the agent needs a simple, auditable example of:

- loading a template from the immutable skill definition directory
- reading reference guidance before execution
- writing all generated artifacts into a disposable workspace

## Inputs

`run_report.py` accepts:

- `--topic`: short topic string for the report title
- `--notes`: optional free-form notes appended to the report body

## Outputs

The script writes `report.md` into the current working directory. The MCP server creates that working directory under `/workspace/...` for each execution.

## Constraints

- Scripts must not write back into `/app/skills`
- Skill assets and references are treated as read-only inputs
- All mutable output belongs under the temporary run workspace
