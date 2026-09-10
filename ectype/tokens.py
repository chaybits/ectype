"""Token counting and token-exact truncation.

tiktoken (cl100k_base) when installed; otherwise a chars/4 heuristic that every caller labels as
an estimate. Special-token strings inside transcripts ("<|endoftext|>") are counted as plain text.
"""
from __future__ import annotations

CHARS_PER_TOKEN = 4          # heuristic used only when tiktoken is absent
_ENC = None
_TRIED = False


def _encoder():
    global _ENC, _TRIED
    if not _TRIED:
        _TRIED = True
        try:
            import tiktoken  # type: ignore
        except ImportError:                 # the optional extra is simply not installed: estimate, quietly
            _ENC = None
            return _ENC
        try:
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception as e:              # noqa: BLE001, installed but unusable (broken wheel, no BPE cache and no network)
            import sys
            print(f"ectype: tiktoken is installed but cl100k_base failed to load ({type(e).__name__}: {e}); "
                  f"token counts are estimates", file=sys.stderr)
            _ENC = None
    return _ENC


def count(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return max(1, len(text) // CHARS_PER_TOKEN)


def is_exact() -> bool:
    return _encoder() is not None


def truncate(text: str, n: int) -> tuple[str, int, int]:
    """Keep the first `n` tokens of `text`.

    Returns (kept_text, tokens_cut, tokens_total); (text, 0, total) when it fits. The total comes
    from the same encoding pass; a caller that needs it must not tokenize the text a second time
    (the cap path once did, for every capped result on every render).
    """
    if n <= 0 or not text:
        return text, 0, count(text)
    enc = _encoder()
    if enc is not None:
        ids = enc.encode(text, disallowed_special=())
        if len(ids) <= n:
            return text, 0, len(ids)
        return enc.decode(ids[:n]), len(ids) - n, len(ids)
    total = max(1, len(text) // CHARS_PER_TOKEN)
    keep = n * CHARS_PER_TOKEN
    if len(text) <= keep:
        return text, 0, total
    return text[:keep], max(1, (len(text) - keep) // CHARS_PER_TOKEN), total
