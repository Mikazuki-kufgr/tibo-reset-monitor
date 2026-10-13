"""Small conservative English/Chinese grammar; unknown wording stays quiet."""
from __future__ import annotations
import re
from datetime import timedelta
from .model import Post, Decision, parse_time, iso, beijing

def rx(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))

def timing(text: str, published: str) -> tuple[str, str | None]:
    # Only explicit ISO offsets or UTC relative times become exact times.
    m = re.search(r'\b(20\d\d-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d))\b', text)
    if m:
        try:
            t = iso(parse_time(m[1]))
            return f'原帖明确时间：{beijing(t)}（非个人账号到账保证）', t
        except ValueError:
            return '原帖时间格式异常；不提供截止时间', None
    m = re.search(r'\b(today|tomorrow)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+UTC\b', text, re.I)
    if m:
        day, hour, minute, half = m.groups()
        h = int(hour)
        if half:
            if not 1 <= h <= 12:
                return '原帖时间超出范围；不提供截止时间', None
            h = h % 12 + (12 if half.lower() == 'pm' else 0)
        mi = int(minute or 0)
        if h > 23 or mi > 59:
            return '原帖时间超出范围；不提供截止时间', None
        p = parse_time(published)
        t = iso((p + timedelta(days=day.lower() == 'tomorrow')).replace(hour=h, minute=mi, second=0, microsecond=0))
        return f'按原帖明确UTC日期：{beijing(t)}（非个人账号到账保证）', t
    if rx(r'\b(EOD|end of day|midnight|today|tomorrow|PST|PDT|PT)\b|今天|明天|日终', text):
        return '原帖含相对/模糊时间；请看原帖，不提供北京时间截止点', None
    return '原帖没有精确重置时间；不提供截止时间', None

def classify(post: Post, context: Post | None = None, inherited: Decision | None = None) -> Decision:
    if not post.verified:
        return Decision('REVIEW', 'unverified_author_or_text')
    text = post.text.replace('’', "'").replace('‘', "'")
    low = text.lower()
    if context and (not context.verified or context.author_id != post.author_id):
        context = None
    if context:
        # Old threads and quotes cannot create a new reset announcement.
        age = (parse_time(post.published) - parse_time(context.published)).total_seconds()
        if age < 0 or age > 72 * 3600:
            context = None
    context_decision = (inherited or classify(context)) if context else Decision()
    contextual_followup = context_decision.kind in ('BANKED','UPCOMING','COMPLETED') and rx(r'^\s*(?:done[.!]|completed[.!]|will be there|all propagated|confirmed landed)', low)
    relevant = rx(r'\bresets?\b|重置|propagated|landed across all accounts', text) or contextual_followup
    if not relevant:
        return Decision()
    if rx(r'password|database|factory reset|server reboot|密码|数据库|恢复出厂', low):
        return Decision('IGNORE', 'unrelated_reset_domain')
    reset_clauses = ' '.join(part for part in re.split(r'[.!?\n]+', low) if rx(r'\bresets?\b|重置', part)) or low
    # Apply to the author's own text, never quoted words of a third party.
    if rx(r"\b(?:no|not|never|won't|will not|can't|cannot|don't|didn't)\b.{0,65}\b(?:reset|resetting|propagat|processed|landed)|\breset\b.{0,25}\b(?:not|never)\b|\bnot all propagated\b|今天不重置|不会重置|未完成|没有重置", low):
        return Decision('IGNORE', 'negative_or_failed_reset')
    if rx(r'\b(yesterday|last week|last month|previous reset|remember when|looking back|historical|recap|in retrospect)\b|上周|昨天|历史|回顾', reset_clauses):
        return Decision('IGNORE', 'historical_recap')
    # Questions/conditional games are not commitments. A hedged paragraph
    # followed by an explicit completed clause is allowed below.
    done_clause = rx(r'\breset(?:s)?\s+(?:has|have)\s+been\s+processed\b|\breset(?:s)?\s+(?:is|are)\s+(?:complete|completed)\b|\breset(?:s)?\s+all\s+propagated\b|全局.*重置.*(?:完成|已生效)', low)
    # Qualify the actual assertion, not an unrelated preamble about products.
    action_parts = [part for part in re.split(r'[.!\n]+',low) if rx(r'reset|重置|propagat|landed across',part) and rx(r'processed|complete|propagat|will reset|by eod|landing|landed|已完成|将.*重置',part)]
    for part in action_parts:
        if rx(r'\b(maybe|perhaps|probably|possibly|might|could|if|unless|hope|hopefully|seems|apparently|i think|i heard|someone says|someone said|people say|rumou?r|guess)\b|也许|可能|据说|猜测|如果|希望|似乎',part):
            return Decision('REVIEW','tentative_or_reported_reset_assertion')
        if '?' in part and not contextual_followup:
            return Decision('REVIEW','question_not_announcement')
    if not done_clause and rx(r'\b(probability|forecast|odds|chance|might|maybe|perhaps|probably|could|if|whenever|the day we|from there onwards|each day|either|28.days)\b|概率|预测|也许|可能|每天|28天', reset_clauses):
        return Decision('IGNORE', 'prediction_condition_or_general_rule')
    if '?' in text and not done_clause and not rx(r'\bwe will\b', low) and not contextual_followup:
        return Decision('REVIEW', 'question_not_announcement')
    bank = rx(r'banked\s+reset|reset\s+(?:bank|credit|voucher|coupon)|储存式|重置券', low)
    bank_context = context_decision.kind == 'BANKED'
    if bank or bank_context:
        affirmative = rx(r'loading|loaded|grant(?:ed|ing)?|giving|available|arriv|lands?|landed|will be there|will.{0,30}(?:reset|give|grant)|发放|领取|可用|到账', low)
        if affirmative:
            note, exact = timing(text, post.published)
            return Decision('BANKED', 'banked_grant_separate_from_global', time_note=note, effective_at=exact, context_id=context.id if bank_context else None)
        return Decision('REVIEW', 'banked_mention_without_grant')
    reset = rx(r'\bresets?\b|重置', low)
    global_scope = rx(r'\bglobal\b|\bfull reset\b|\breset.{0,60}\ball\b|\ball.{0,60}\breset\b|全局|所有.*(?:codex|work|额度)|(?:codex|work).*所有', low)
    explicit_product = rx(r'codex|chatgpt\s*work|usage limits|rate.?limits|额度', low)
    context_global = context_decision.kind in ('UPCOMING', 'COMPLETED')
    if post.author == 'openai' and not rx(r'codex|chatgpt\s*work', low) and not context_global:
        return Decision('REVIEW', 'official_post_product_scope_unclear')
    if rx(r'just my|my account|my quota|only (?:some|a few)|some users|部分|我的账号', low):
        return Decision('IGNORE', 'individual_or_partial_scope')
    if reset and (done_clause or (rx(r'all propagated|confirmed.{0,15}(?:complete|done)|已完成', low) and (global_scope or context_global))):
        return Decision('COMPLETED', 'official_completion_statement', time_note='官方声明重置已完成；发布时间不等于每个账号实际到账时刻', context_id=context.id if context_global else None)
    if not reset and context_global and rx(r'^\s*(?:all propagated|confirmed landed across all accounts|done[.!]|completed[.!])', low):
        return Decision('COMPLETED', 'completion_with_verified_parent', time_note='官方声明重置已完成；发布时间不等于每个账号实际到账时刻', context_id=context.id)
    upcoming = rx(r'\bglobal reset\s+(?:by|landing|tomorrow|today|soon|at|in)\b|\b(?:full reset|reset).{0,45}\b(?:is landing|will (?:land|happen)|landing (?:today|tomorrow)|by (?:EOD|midnight))\b|\bwe(?: are going to| will| will be)\s+(?:reset|resetting)\b|将.*重置|即将.*重置', low)
    if reset and upcoming and (global_scope or explicit_product or context_global):
        note, exact = timing(text, post.published)
        return Decision('UPCOMING', 'explicit_global_commitment', time_note=note, effective_at=exact, context_id=context.id if context_global else None)
    if reset:
        return Decision('REVIEW', 'insufficient_scope_or_commitment')
    return Decision()

def classify_chain(post: Post, by_id: dict[str, Post], depth: int = 3) -> Decision:
    ref = post.quote or post.parent
    context = by_id.get(ref) if ref and depth > 0 else None
    inherited = classify_chain(context, by_id, depth - 1) if context else None
    return classify(post, context, inherited)
