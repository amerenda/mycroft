"""Tests for report title/summary extraction (tool leakage + filler tolerant)."""

from coordinator.report_metadata import extract_title_summary, unwrap_report_markdown_body


def test_unwrap_submit_report_json_prefix():
    raw = (
        'submit_report[ARGS]{"content": "# DNS over HTTPS (DoH)\\n\\n## Overview\\nDNS over HTTPS..."}'
    )
    body = unwrap_report_markdown_body(raw)
    assert body.startswith("# DNS over HTTPS (DoH)")
    assert "## Overview" in body


def test_title_from_unwrapped_heading_not_tool_prefix():
    raw = (
        'submit_report[ARGS]{"content": "# DNS over HTTPS (DoH)\\n\\n## Overview\\nBody."}'
    )
    title, _ = extract_title_summary(raw)
    assert title == "DNS over HTTPS (DoH)"


def test_skip_fluff_before_real_heading():
    raw = (
        "Here is the formatted and polished Markdown version of the requested summary, based on the "
        "research pack.\n\n"
        "# QUIC vs HTTP/2\n\n## Summary\nDone."
    )
    title, _ = extract_title_summary(raw)
    assert title == "QUIC vs HTTP/2"


def test_json_fence_with_content_key():
    raw = '```json\n{"name": "submit_report", "arguments": {"content": "# Title\\n\\nText"}}\n```'
    title, _ = extract_title_summary(raw)
    assert title == "Title"


def test_plain_markdown_unchanged():
    raw = "# Plain\n\n## Summary\nHi."
    title, summary = extract_title_summary(raw)
    assert title == "Plain"
    assert "Hi." in summary
