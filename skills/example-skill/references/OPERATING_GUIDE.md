# Example Skill Operating Guide

This example skill is intentionally simple.

Execution contract:

- The MCP server launches the script with the current working directory set to a fresh temporary directory under `/workspace`.
- The skill definition root is exposed to the script through the `SKILL_ROOT` environment variable.
- The run workspace path is exposed through the `SKILL_WORKSPACE` environment variable.

Recommended usage:

1. Read `SKILL.md`.
2. Check the template in `assets/report_template.md`.
3. Run `run_report.py` only when a report artifact is required.

Expected artifact:

- `report.md`

Expected behavior:

- The script should succeed with only `--topic`.
- `--notes` is optional and appended verbatim.
