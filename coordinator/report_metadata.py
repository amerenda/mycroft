"""Derive report title and summary from agent output.

Tolerates tool-call text leaked into the final KB blob (e.g. submit_report JSON)
and skips common filler lines so list titles stay human-readable.
"""

from __future__ import annotations

import json
import re
from typing import Final

_FLUFF_TITLE: Final[re.Pattern[str]] = re.compile(
    r"(?i)^("
    r"here is|here's|below is|the following|below you will|i have (prepared|formatted)|"
    r"i've (prepared|formatted)|this is the|as requested|sure!|"
    r"below is an updated|the (formatted|polished) (markdown|report)"
    r")\b"
)

_JSON_FENCE: Final[re.Pattern[str]] = re.compile(
    r"^```(?:json)?\s*\r?\n(.*?)\r?\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

_SKIP_H2_FOR_TITLE: Final[frozenset[str]] = frozenset(
    {"summary", "overview", "introduction", "conclusion", "references", "sources", "citations"}
)


def unwrap_report_markdown_body(raw: str) -> str:
    """Return markdown meant for the user: unwrap JSON tool payloads and fences."""
    t = raw.strip()
    if not t:
        return t

    m = _JSON_FENCE.match(t)
    if m:
        inner = m.group(1).strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                c = obj.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
                args = obj.get("arguments")
                if isinstance(args, dict):
                    ac = args.get("content")
                    if isinstance(ac, str) and ac.strip():
                        return ac.strip()
        except json.JSONDecodeError:
            if inner.lstrip().startswith("#") or inner.lstrip().startswith("##"):
                return inner
        t = inner

    low = t[:800].lower()
    if "submit_report" in low or ('"content"' in t and "{" in t):
        brace = t.find("{")
        if brace >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(t[brace:])
                if isinstance(obj, dict):
                    c = obj.get("content")
                    if isinstance(c, str) and c.strip():
                        return c.strip()
                    args = obj.get("arguments")
                    if isinstance(args, dict):
                        ac = args.get("content")
                        if isinstance(ac, str) and ac.strip():
                            return ac.strip()
            except json.JSONDecodeError:
                pass

    return t


def _line_is_fluff_for_title(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if _FLUFF_TITLE.match(s):
        return True
    if "submit_report" in s.lower():
        return True
    if s.startswith("```"):
        return True
    if s.startswith("{") and '"arguments"' in s:
        return True
    return False


def extract_title_summary(content: str) -> tuple[str, str]:
    """Pick title from the first real markdown heading; summary from ## Summary if present."""
    body = unwrap_report_markdown_body(content)
    title = "Research Report"

    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("# "):
            frag = line[2:].strip().strip("*").strip()
            if frag and not _line_is_fluff_for_title(frag):
                title = frag[:500]
                break
    else:
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("## "):
                continue
            frag = line[3:].strip().strip("*").strip()
            if not frag or _line_is_fluff_for_title(frag):
                continue
            if frag.lower() in _SKIP_H2_FOR_TITLE:
                continue
            title = frag[:500]
            break

    summary = ""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## summary"):
            summary_lines: list[str] = []
            for sl in lines[i + 1 :]:
                if sl.startswith("## "):
                    break
                if sl.strip():
                    summary_lines.append(sl.strip())
            summary = " ".join(summary_lines)[:500]
            break
    if not summary:
        summary = body[:300]

    return title, summary
