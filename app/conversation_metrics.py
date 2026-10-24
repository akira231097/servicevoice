"""Numeric phone-reply length evidence; no transcript or business authority."""
import re
import statistics
from .backchannel_catalog import PHRASES

EXEMPT_TOOLS=frozenset({'search_availability','prepare_booking','prepare_first_available_booking',
    'prepare_reschedule','prepare_cancellation','confirm_action'})

def reply_lengths(evidence):
    rows=sorted((v for k,v in evidence.items() if re.fullmatch(r'(?:turn|tool):\d+',k)
                 and isinstance(v,dict)),key=lambda v:v.get('sequence',-1))
    cues=sorted((re.findall(r"\b\w+(?:['’]\w+)?\b",p.casefold()) for p in PHRASES.values()),key=len,reverse=True)
    ordinary=[];exempt=[];words=[];seen_user=False;exception=False
    def finish():
        if words and seen_user:(exempt if exception else ordinary).append(len(words))
        words.clear()
    for row in rows:
        if row.get('role')=='user':
            finish();seen_user=True;exception=False
        elif row.get('role')=='assistant':
            tokens=re.findall(r"\b\w+(?:['’]\w+)?\b",row.get('text','').casefold())
            while tokens:
                prefix=next((c for c in cues if tokens[:len(c)]==c),None)
                if prefix is None:break
                tokens=tokens[len(prefix):]
            words.extend(tokens)
        elif (row.get('name') in EXEMPT_TOOLS and row.get('gateway_issuance_verified') is True
              and isinstance(row.get('server_result'),dict) and row['server_result'].get('ok') is True):
            exception=True
    finish()
    return {'ordinary_replies':len(ordinary),'readback_or_options_replies':len(exempt),
        'median_words':statistics.median(ordinary) if ordinary else None,
        'maximum_words':max(ordinary) if ordinary else None,
        'over_20_words':sum(n>20 for n in ordinary),
        'within_20_words_fraction':sum(n<=20 for n in ordinary)/len(ordinary) if ordinary else None,
        'scope':'Stored transcript word counts, not spoken duration. Greeting and approved hold prefixes excluded; verified readback/options/result turns exempt. Interrupted replies may be partial.'}
