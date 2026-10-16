"""Phase 6c contradiction detection — lexical, stdlib-only, no segmenter dep.

Detects stored claims that disagree: same subject, incompatible polarity.
Proposes conflicts; never auto-resolves. The engine surfaces candidates,
a human or governed tool decides via supersede/reject.

Deliberately lexical (substring + polarity word lists incl. Thai), not
embedding-based: the recall baseline showed paraphrase=0.50, so a vector
layer would add a dependency without solving the underlying problem first.
No PyThaiNLP unless the owner approves a dep (decision 2026-10-03).
"""
import re
import unicodedata

# Polarity markers: (positive_words, negative_words) per language lane.
# A claim containing a negative marker with the same subject as a claim
# without one (or with the opposite marker) is a contradiction candidate.
_NEGATIVE_EN = frozenset({
    'not', 'no', 'never', 'cannot', "can't", "don't", 'neither', 'nor',
    'without', 'against', 'forbidden', 'prohibited', 'must not', 'do not',
    'is not', 'are not', 'was not', 'were not', 'will not', 'cannot',
})
_NEGATIVE_TH = frozenset({
    'ไม่', 'ห้าม', 'ไม่ใช่', 'ไม่มี', 'ไม่ได้', 'อย่า', 'ยกเลิก',
})

# Numeric claims: "port 20128" vs "port 8080" on the same subject disagree.
# Standalone numbers only: digits glued inside a word (the "9" in "9router",
# the "1" in "v1") are identifiers, not claims. Lookarounds keep stdlib-only.
_NUMBER_RE = re.compile(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)*(?![A-Za-z0-9_])')


def _tokens(text):
    """Word-ish tokens: Unicode letters/numbers, lowercased, NFC."""
    text = unicodedata.normalize('NFC', str(text or '').lower())
    tokens, buf = [], []
    for ch in text:
        if ch == '_' or unicodedata.category(ch)[:1] in ('L', 'N', 'M'):
            buf.append(ch)
        elif buf:
            tokens.append(''.join(buf))
            buf = []
    if buf:
        tokens.append(''.join(buf))
    return tokens


def subject_key(content, max_tokens=4):
    """Coarse subject signature: first content words after stripping
    polarity markers, particles, and punctuation.

    Not a parser — a blocking key. Two claims sharing a key are compared;
    claims with different keys are never paired. Precision over recall:
    missing a contradiction is cheaper than a false conflict.
    """
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = 4
    particles = frozenset({
        'the', 'a', 'an', 'is', 'are', 'was', 'be', 'use', 'uses', 'used',
        'using', 'do', 'does', 'did', 'ต่อไปนี้', 'สำหรับ', 'นี้',
        'คือ', 'ที่', 'ใน', 'ของ', 'และ', 'หรือ', 'จะ', 'ได้',
        'นะคะ', 'นะครับ', 'ค่ะ', 'ครับ',
    })
    def _glued_negative(token):
        # Thai has no spaces: 'ห้ามใช้' is one token containing 'ห้าม'.
        # Strip a leading negative marker so the subject key matches the
        # affirming claim's key.
        for marker in _NEGATIVE_TH:
            if token.startswith(marker) and len(token) > len(marker):
                return token[len(marker):]
        # 'ต่อไปนี้' (from now on) glues the same way; it is procedural,
        # not subject matter.
        if token.startswith('ต่อไปนี้') and len(token) > len('ต่อไปนี้'):
            return token[len('ต่อไปนี้'):]
        if token == 'ต่อไปนี้':
            return ''
        return token

    tokens = []
    for t in _tokens(content):
        if t in particles or t in _NEGATIVE_EN or t in _NEGATIVE_TH:
            continue
        t = _glued_negative(t)
        if not t:
            continue
        if t[:1].isdigit():
            # Numbers are compared separately (numeric reason); keeping them
            # in the key would split 'port 20128' from 'port 8080' apart.
            continue
        # Strip English verb inflections so 'uses'/'use' share a key.
        if len(t) > 4 and t.endswith('es'):
            t = t[:-2]
        elif len(t) > 3 and t.endswith('s'):
            t = t[:-1]
        tokens.append(t)
    return ' '.join(tokens[:max_tokens])


def polarity(content):
    """+1 (affirming), -1 (negating), 0 (no marker found).

    Numeric-only claims return 0 — numbers are compared separately.
    """
    text = unicodedata.normalize('NFC', str(content or '').lower())
    neg_hits = sum(1 for w in _NEGATIVE_EN if w in text)
    neg_hits += sum(1 for w in _NEGATIVE_TH if w in text)
    if neg_hits:
        return -1
    return 0


def numbers(content):
    """Normalized number set for numeric-agreement comparison."""
    text = str(content or '')[:10000]
    return frozenset(_NUMBER_RE.findall(text))


# Exclusive-choice word sets: naming one member implicitly rejects the
# others on the same subject ("converse in English" vs a memory stating
# Thai). Language lanes are the fleet-case; protocol/method pairs follow
# the same shape. Deliberately small: each set must be unambiguous in
# the fleet domain, never generic adjectives.
_EXCLUSIVE_SETS = (
    frozenset({'thai', 'english'}),
    frozenset({'mcp', 'bridge'}),
)


def _nearest_member_to_anchor(toks, excl, other_tok):
    """Exclusive-set member closest to any shared anchor token.

    ``toks`` is the pre-tokenized text (ordered); callers tokenize once and
    reuse, so a row loop never re-tokenizes the same side repeatedly.
    Anchor = token present on both sides (len>=4, not a pure number).
    Returns (member, distance) or (None, None) when neither exists.
    Nearest wins: a compound fact ("Thai with P Choke; English for
    bots") scopes each member to its own clause instead of letting a
    far member pollute the comparison.
    """
    # Anchors are shared NON-member tokens: the member itself is never
    # its own anchor (else distance is trivially 0 on both sides and
    # every comparison ties).
    anchors = {i for i, t in enumerate(toks)
               if len(t) >= 4 and t in other_tok and t not in excl
               and not _NUMBER_RE.fullmatch(t)}
    if not anchors:
        return None, None
    best, best_d = None, None
    for i, t in enumerate(toks):
        if t in excl and anchors:
            d = min(abs(i - ax) for ax in anchors)
            if best_d is None or d < best_d:
                best, best_d = t, d
    return best, best_d


def _exclusive_member_conflict(query_tokens, ftokens, query_tok, ftok,
                               choice_sets=None):
    """True when an exclusive-choice set has different members near the anchor.

    ``choice_sets`` limits the scan to sets the query actually names (the
    only sets that can produce a conflict); None scans all sets.
    """
    if choice_sets is None:
        choice_sets = _EXCLUSIVE_SETS
    for excl in choice_sets:
        # Scope the exclusivity to the shared subject window: the member
        # must sit within a few tokens of a shared anchor on BOTH sides.
        # ("converse in English with P Choke" vs a fact that also says
        # "always in English" for bot-to-bot: bag-of-words would miss,
        # but near the shared anchor P-Choke only Thai appears.)
        qm, _ = _nearest_member_to_anchor(query_tokens, excl, ftok)
        fm, _ = _nearest_member_to_anchor(ftokens, excl, query_tok)
        if qm is not None and fm is not None and qm != fm:
            return True
    return False


#: Cheap pre-screen regexes for the demotion row loop: a row can only
#: contradict via an exclusive-choice member (substring present) or a
#: negated phrase (some negative marker present). Deliberately an
#: OVER-approximation (substring, not token equality) — a false positive
#: merely pays for the full check, while the full check still decides.
#: Under-approximating here would silently change recall behavior.
_NEG_MARKER_RE = re.compile(
    '|'.join(re.escape(w) for w in sorted(
        _NEGATIVE_EN | _NEGATIVE_TH, key=len, reverse=True)),
    re.IGNORECASE)
_EXCL_MEMBER_RE = re.compile(
    '|'.join(re.escape(w) for w in sorted(
        {m for s in _EXCLUSIVE_SETS for m in s}, key=len, reverse=True)),
    re.IGNORECASE)


#: Bounded memo for the demotion pre-screen (content -> screen flags).
#: Row contents repeat across queries while the store is stable, and a
#: string-hash dict lookup is far cheaper than re-scanning marker regexes.
#: Pure function of the content string (memory rows are immutable), so the
#: cache can never go stale; cleared wholesale on overflow. Very long rows
#: (rare) bypass the cache so it cannot grow large.
_SCREEN_CACHE: dict = {}
_SCREEN_CACHE_MAX = 4096
_SCREEN_CACHE_CONTENT_MAX = 512


def _screen_flags(content):
    """(has_neg_marker, has_excl_member) for one row content, memoized."""
    if len(content) > _SCREEN_CACHE_CONTENT_MAX:
        return (bool(_NEG_MARKER_RE.search(content)),
                bool(_EXCL_MEMBER_RE.search(content)))
    flags = _SCREEN_CACHE.get(content)
    if flags is None:
        flags = (bool(_NEG_MARKER_RE.search(content)),
                 bool(_EXCL_MEMBER_RE.search(content)))
        if len(_SCREEN_CACHE) >= _SCREEN_CACHE_MAX:
            _SCREEN_CACHE.clear()
        _SCREEN_CACHE[content] = flags
    return flags


def _row_may_contradict(content, choice_active, phrase_active):
    """True when a row could possibly fire an exclusive/phrase signal."""
    if not choice_active and not phrase_active:
        return False
    has_neg, has_member = _screen_flags(content)
    if phrase_active and has_neg:
        return True
    if choice_active and has_member:
        return True
    return False


def _query_phrase_pairs(query_tokens):
    """Contiguous query bigrams worth phrase-matching (non-trivial, non-numeric).

    Only pairs whose first token is >=4 chars and second >=3 chars count:
    short function words ("with", "in", "to") glue sentences together and
    would match prose by accident. Digits never form phrase pairs.
    """
    return {
        (t1, t2) for t1, t2 in zip(query_tokens, query_tokens[1:])
        if len(t1) >= 4 and len(t2) >= 3
        and not _NUMBER_RE.search(t1) and not _NUMBER_RE.search(t2)
    }


def _fact_negates_phrase(ftokens, phrase_pairs, query):
    """True when the fact negates a contiguous phrase the query names.

    Pattern: the fact carries a query bigram with a negative marker attached
    directly ("A B does not ...", "A B never ...", "not A B ..."). The query
    must be affirmative: a negated query already states what it expects, so
    phrase demotion applies only to the affirmative->negated direction.
    Precision-first: exact contiguous bigrams only, so prose never matches
    by accident.
    """
    if polarity(query) != 0:
        return False
    for i in range(len(ftokens) - 1):
        if (ftokens[i], ftokens[i + 1]) not in phrase_pairs:
            continue
        if i > 0 and (ftokens[i - 1] in _NEGATIVE_EN
                      or ftokens[i - 1] in _NEGATIVE_TH):
            return True
        for w in ftokens[i + 2:i + 4]:
            if w in _NEGATIVE_EN or w in _NEGATIVE_TH:
                return True
    return False


def query_fact_incompatible(query, fact, query_tok=None, query_numbers=None,
                            phrase_pairs=None, query_tokens=None,
                            choice_sets=None):
    """True when a query and a fact share a subject anchor but state

    incompatible values: disjoint standalone numbers, different members of
    an exclusive-choice set, or a contiguous phrase the fact negates while
    the query affirms it. Query-side companion to is_contradiction_pair
    (which pairs two stored claims via the strict subject key): queries are
    short and word order differs, so pairing uses shared rare anchors
    instead of the key.

    Precision over recall: False means "no signal", never "compatible".

    ``query_tok`` (a set of _tokens(query)), ``query_numbers``
    (numbers(query)), ``query_tokens`` (the ordered tokens) and
    ``phrase_pairs`` (_query_phrase_pairs over the ordered tokens) may be
    precomputed by the caller so a row loop never re-tokenizes the query on
    every fact. ``choice_sets`` limits the exclusive scan to sets the query
    names (None scans all).
    """
    if query_tokens is None:
        query_tokens = _tokens(query)
    if query_tok is None:
        query_tok = set(query_tokens)
    if query_numbers is None:
        query_numbers = numbers(query)
    ftokens = _tokens(fact)
    ftok = set(ftokens)
    # shared anchor: a non-trivial token on both sides (length>=4 skips
    # particles/prepositions; digits handled by the numeric check below).
    anchor_shared = any(
        len(t) >= 4 and t in ftok
        for t in query_tok
        # identifiers (9router, lnwjud_call) anchor; pure numbers do not.
        if not _NUMBER_RE.fullmatch(t)
    )
    if not anchor_shared:
        return False
    fn = numbers(fact)
    if query_numbers and fn and not (query_numbers & fn):
        return True
    if _exclusive_member_conflict(query_tokens, ftokens, query_tok, ftok,
                                  choice_sets=choice_sets):
        return True
    if phrase_pairs and polarity(fact) != 0:
        return _fact_negates_phrase(ftokens, phrase_pairs, query)
    return False


def is_contradiction_pair(a, b):
    """True if two contents disagree: same subject key, opposite polarity,
    or same subject with disjoint non-empty number sets.

    Returns (is_contradiction, reason).
    """
    ska = subject_key(a)
    if not ska or ska != subject_key(b):
        return False, 'different_subject'
    pa, pb = polarity(a), polarity(b)
    if pa != pb and (pa, pb) in ((0, -1), (-1, 0)):
        return True, 'polarity'
    na, nb = numbers(a), numbers(b)
    if na and nb and not (na & nb):
        return True, 'numeric'
    return False, 'agree_or_unknown'
