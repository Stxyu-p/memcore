"""MemCore recall — query tokenization: fleet aliases, Thai bigrams, FTS5 query building."""
from __future__ import annotations

import unicodedata

from . import ablation as _ablation




# —— Lane 3.2: Fleet alias map (static, query-side only).
# Add entries ONLY from failing baseline paraphrase queries.
# Substring containment on lowered raw query, so glued Thai matches.
_FLEET_ALIASES = {
    'ai gateway': '9router',
    'ทีม': 'fleet roster',
    'พี่โชค': 'thai',
    'สแกน': 'scan pacing',
}



def _expand_aliases(query: str) -> str:
    """Expand known fleet aliases in the raw query before tokenization.

    Substring match on lowered query; matched values appended with spaces.
    Eval-only: MEMCORE_ABLATE_ALIAS_EXPANSION=1 returns query unchanged.
    """
    try:
        if _ablation.is_alias_expansion_ablated():
            return query
    except Exception:
        pass
    q = str(query).lower()
    expansions = []
    for key, val in _FLEET_ALIASES.items():
        if key in q:
            expansions.append(val)
    if expansions:
        return query + ' ' + ' '.join(expansions)
    return query



def _thai_bigrams(token: str) -> list[str]:
    """Generate character bigrams from a Thai/Unicode token (ord>127, len>=4).

    Used as extra OR-terms in FTS5 query (as prefix terms) and exact-fallback
    (as substring ORs) to catch glued-word substrings. Capped by caller
    (total OR-terms <= 32).
    Eval-only: MEMCORE_ABLATE_THAI_BIGRAM=1 returns [].
    """
    try:
        if _ablation.is_thai_bigram_ablated():
            return []
    except Exception:
        pass
    if len(token) < 4:
        return []
    # Only emit for tokens containing non-ASCII
    if all(ord(ch) <= 127 for ch in token):
        return []
    return [token[i:i+2] for i in range(len(token) - 1)]



def _query_tokens(query: str) -> list[str]:
    """Split a raw query into word-ish tokens (Unicode L/N/M + underscore).

    Shared by _fts_query and the search exact-fallback so both see the
    same token stream.
    """
    tokens, buf = [], []
    for ch in str(query):
        category = unicodedata.category(ch)
        if ch == '_' or category[:1] in ('L', 'N', 'M'):
            buf.append(ch)
        elif buf:
            tokens.append(''.join(buf))
            buf = []
    if buf:
        tokens.append(''.join(buf))
    return tokens



def _fts_query(query: str) -> str:
    """Sanitize a raw Unicode user string into a safe FTS5 expression.

    Keep Unicode letters/numbers/marks plus underscore, split on punctuation,
    then quote each token. For Thai/Unicode tokens (ord>127, len>=4) also
    emit character bigrams as extra prefix OR-terms ("bg"*) to catch
    glued-word substrings — plain "bg" never matches in FTS5 unicode61,
    only prefix "bg"* matches token starts.
    Total OR-terms capped at 32 (ponytail: O(query_len) terms; upgrade path
    is a proper segmenter only if owner approves a dep).
    """
    tokens = _query_tokens(query)
    if not tokens:
        return ''

    # Build OR-terms: each token + its Thai bigrams as prefix terms (if applicable)
    terms = []
    for token in tokens:
        terms.append('"%s"' % token)
        # Lane 3.1: add Thai char bigrams as prefix terms for glued words
        for bg in _thai_bigrams(token):
            terms.append('"%s"*' % bg)
            if len(terms) >= 32:
                break
        if len(terms) >= 32:
            break

    return ' OR '.join(terms)



def _extract_non_ascii_substr_terms(user_query: str) -> list[str]:
    """Match terms for the Unicode substring fallback lane (single pass).

    unicode61 never segments glued Thai, so non-ASCII queries need a
    substring fallback. Match terms = NON-ASCII word tokens of the
    PRE-expansion query + their char bigrams. ASCII tokens are deliberately
    excluded — FTS already handles them, and instr-ORing them (e.g. 'agent'
    matching 'agents') flips negation guards. Alias expansion can never break
    the verbatim check (pre-expansion query only); glued->spaced still
    bridges via bigrams. Capped at MAX_SUBSTR_TERMS.
    """
    if not any(ord(ch) > 127 for ch in user_query):
        return []
    terms: list[str] = []
    seen_terms: set[str] = set()
    for token in _query_tokens(user_query):
        if all(ord(ch) <= 127 for ch in token) or token in seen_terms:
            continue
        seen_terms.add(token)
        terms.append(token)
        for bg in _thai_bigrams(token):
            if bg in seen_terms:
                continue
            seen_terms.add(bg)
            terms.append(bg)
            if len(terms) >= MAX_SUBSTR_TERMS:
                break
        if len(terms) >= MAX_SUBSTR_TERMS:
            break
    return terms


#: Cap on `instr(v.content, ?)` OR-terms in the Thai substring lane. Each term
#: is a full table scan of the joined version rows, so this is the single
#: biggest lever on Thai-query latency (measured: a 32-term glued Thai query
#: costs ~5ms, ASCII ~1ms). Lower it if recall ever degrades for long queries.
MAX_SUBSTR_TERMS = 32
