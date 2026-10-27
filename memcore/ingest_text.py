"""MemCore journal — text classification: event vocabulary, chat/durable-signal heuristics."""
import re

from . import core



EVENT_TYPES = {'turn', 'memory_write', 'delegation', 'session_end', 'manual'}

MAX_FIELD_CHARS = 65536

MAX_ANALYSIS_CANDIDATE_CHARS = 4000

SEMANTIC_VERDICTS = {'remember', 'ignore', 'defer'}

SEMANTIC_QUEUE_DECISIONS = {'semantic_review_required', 'semantic_deferred'}


_TRIVIAL_RE = re.compile(
    r'^(?:ok|okay|yes|no|thanks|thank you|hi|hey|hello|continue|next|done|test|'
    r'(?:โอเค|ขอบคุณ|สวัสดี|ทดสอบ|เทส|ต่อ|ต่อเลย|ได้|ดี)\s*(?:นะคะ|นะครับ|ค่ะ|ครับ|คะ)?|'
    r'ครับ|ค่ะ)[\s!?.…]*$', re.IGNORECASE
)

_EXPLICIT_MEMORY_RE = re.compile(
    r'(?:^|\b)(?:please\s+)?remember(?:\s+that)?\b|'
    r'\bfrom now on\b|\bi (?:prefer|always use)\b|\bwe (?:decided|agreed|chose)\b|'
    r'(?:^|\s)(?:จำไว้(?:ว่า)?|ช่วยจำ(?:ไว้)?(?:ว่า)?|จำว่า|ต่อไปนี้|'
    r'ฉันชอบ|ผมชอบ|ฉันใช้|ผมใช้|เราตกลง(?:กัน)?(?:ว่า)?|เราตัดสินใจ(?:กัน)?(?:ว่า)?)',
    re.IGNORECASE
)

_NEGATIVE_MEMORY_RE = re.compile(
    r'\b(?:without|don\'t|dont|do not|never|not|no need to)\s+(?:explicit\s+)?remember\b|'
    r'(?:ไม่ต้อง|อย่า|ห้าม|ไม่ได้)\s*(?:จำ|ช่วยจำ)',
    re.IGNORECASE
)

_LEADING_REMEMBER_RE = re.compile(
    r'^\s*(?:(?:please\s+)?remember(?:\s+that)?|จำไว้(?:ว่า)?|ช่วยจำ(?:ไว้)?(?:ว่า)?|จำว่า)'
    r'[\s,:-]*', re.IGNORECASE
)

_QUESTION_OR_CHAT_RE = re.compile(
    r'\?|'
    r'(?:^|\s)(?:ไหม|มั้ย|หรือเปล่า|หรือไม่|หรือยัง|ยังไง|อย่างไร|แค่ไหน|อะไรอีก|ลองอธิบาย|ช่วยอธิบาย)'
    r'[\s!?.…]*$',
    re.IGNORECASE
)

_NAVIGATION_PREFIX_RE = re.compile(
    r'^(?:ต่อไป|ถัดไป|ข้อต่อไป|ต่อเลย)\b',
    re.IGNORECASE
)


def _clean_text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise core.MemCoreError('journal content must be a string')
    return value[:MAX_FIELD_CHARS]



def _has_remove_reference(event_type, metadata):
    """True when a memory_write/remove event carries its old_text reference."""
    memory_action = str(metadata.get('action') or '').strip().lower()
    builtin_metadata = metadata.get('builtin_metadata')
    old_text = metadata.get('old_text')
    if not old_text and isinstance(builtin_metadata, dict):
        old_text = builtin_metadata.get('old_text')
    return (
        event_type == 'memory_write' and memory_action == 'remove'
        and isinstance(old_text, str) and bool(old_text.strip())
    )



def classify_user_text(text):
    """Deterministic first-stage gate. It never trusts assistant output."""
    text = (text or '').strip()
    if not text or text.startswith('/') or '<memory-context>' in text.lower():
        return 'ignore', 'non_user_signal', ''
    if _TRIVIAL_RE.fullmatch(text):
        return 'ignore', 'trivial', ''
    if _NEGATIVE_MEMORY_RE.search(text):
        return 'review', 'semantic_review_required', ''
    if _EXPLICIT_MEMORY_RE.search(text):
        candidate = _LEADING_REMEMBER_RE.sub('', text).strip() or text
        if len(candidate) > MAX_ANALYSIS_CANDIDATE_CHARS or '```' in candidate:
            return 'review', 'semantic_review_required', ''
        return 'candidate', 'explicit_durable_signal', candidate
    return 'review', 'semantic_review_required', ''
