"""The Anthropic-shaped content block, which three stores use.

Claude Code writes it natively; Cline and Roo Code (a Cline fork) both keep their conversation in
the same shape, because that is what they send to the API. The pieces they share live here so a
fix lands in all three at once; they were three copies, and the copies had already drifted.

This is vendor-shape knowledge and belongs under `adapters/`, not in the canonical model.
"""
from __future__ import annotations

import json
from typing import Any


def result_text(content: Any) -> str:
    """The text of a `tool_result` block, whatever shape the store put it in.

    `content` is a string, or a list of parts of which only the `{"type": "text"}` ones carry
    anything, or (rarely) a bare object. The three copies of this all did the string and list
    cases and let an object through as a dict, which then sat in `ContentBlock.text` where every
    reader assumes a string (`len(b.text)` raises on it).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
    if isinstance(content, dict):
        return content.get("text") or json.dumps(content, ensure_ascii=False)
    return str(content)
