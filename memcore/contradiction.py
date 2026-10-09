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
    return frozenset(_NUMBER_RE.findall(str(content or '')))


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
