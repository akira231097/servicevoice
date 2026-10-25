"""Narrow explicit English closing shortcut; all ambiguity goes to the model.

Never interprets consent, changes an appointment, or substitutes for a pending
proposal withdrawal. The gateway also requires observed final speech and no
unresolved operation before issuing the existing signed native endCall.
"""
import json
import re

_CLAUSE = re.compile(r"(?:okay|ok|stop|thank you|thanks|that's all(?: for now)?|that is all(?: for now)?|"
    r"nothing else|no more questions|do not book anything|don't book anything|"
    r"i (?:only |just )?wanted to (?:test|try) (?:the|this) (?:call|conversation|agent)|"
    r"goodbye|bye|please (?:end the call|hang up))",re.I)
_END = re.compile(r"(?:goodbye|bye|please end the call|please hang up)[.! ,]*$",re.I)


def candidate(messages):
    if not messages:return None
    # prepare_request has already authenticated every tool/result pair. Only
    # a successful withdrawal may be skipped over; never an accepted action.
    history=messages
    if history[-1].get('role')=='tool':
        if len(history)<3:return None
        previous,last=history[-2:]
        calls=previous.get('tool_calls')
        if not isinstance(calls,list) or len(calls)!=1 or calls[0]['function']['name']!='dismiss_proposal':return None
        try:
            result=json.loads(last['content'])
            if result.get('ok') is not True or 'error' in result:return None
        except (ValueError,TypeError):return None
        history=history[:-2]
    if history[-1].get('role')!='user':return None
    text=history[-1].get('content')
    if not isinstance(text,str) or len(text)>400 or not text.isascii() or not _END.search(text):return None
    # Consume the whole utterance. Quoted, conditional, negated or followed by
    # another request is not a closing command, even if it contains "goodbye".
    remaining=text.strip()
    while remaining:
        match=_CLAUSE.match(remaining)
        if not match:return None
        remaining=remaining[match.end():]
        if remaining and remaining[0] not in ' .!,':return None
        remaining=remaining.lstrip(' .!,')
    return text
