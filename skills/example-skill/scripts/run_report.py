#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a markdown report into the current workspace."
    )
    parser.add_argument("--topic", required=True, help="Report topic.")
    parser.add_argument(
        "--notes",
        default="No notes were provided.",
        help="Optional report notes.",
    )
    args = parser.parse_args()

    skill_root = Path(os.environ["SKILL_ROOT"]).resolve()
    workspace = Path.cwd().resolve()
    template_path = skill_root / "assets" / "report_template.md"
    template = template_path.read_text(encoding="utf-8")

    rendered = (
        template.replace("{{topic}}", args.topic.strip())
        .replace("{{notes}}", args.notes.strip() or "No notes were provided.")
        .strip()
        + "\n"
    )

    output_path = workspace / "report.md"
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
