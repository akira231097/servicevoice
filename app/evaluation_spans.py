"""Content-free, deterministic LangSmith children from recorded component events.

This module performs no requests or business writes. A group is an explicitly
labelled envelope of correlated observed events, not a measured execution stage
or proof of causality. Missing timing never receives a fabricated interval.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
import re
from uuid import UUID, uuid5

from .diagnostics import COMPONENTS, EVENTS, OUTCOMES, safe_attributes

VERSION = "component-spans-v1"
_HASH = re.compile(r"[a-f0-9]{64}")
_TURN = re.compile(r"turn_[a-f0-9]{24}")
_OPERATION = re.compile(r"op_[a-f0-9]{24}")
_DOT_SEGMENT = re.compile(r"\d{8}T\d{12}Z[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


def _uuid(value):
    if type(value) is not str:return None
    try:return str(UUID(value))
    except (ValueError,AttributeError):return None


def _optional_identifier(value,pattern):
    return value if type(value) is str and pattern.fullmatch(value) else None


def _time(value):
    return type(value) in (int,float) and math.isfinite(value) and 1577836800 <= value <= 4102444800


def _duration(value):
    return type(value) in (int,float) and math.isfinite(value) and 0 <= value <= 86_400_000


def _iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec="microseconds")


def _segment(start,run_id):
    return datetime.fromtimestamp(start,timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+run_id


def _project(row,key):
    if not isinstance(row,dict):return None,"invalid_event"
    if row.get("call_key")!=key:return None,"different_call_or_invalid_call_key"
    event_id=_uuid(row.get("event_id"))
    if event_id is None or not isinstance(row.get("config_id"),str) or not _HASH.fullmatch(row["config_id"]):
        return None,"invalid_event_identity"
    if any(type(row.get(field)) is not str or row[field] not in choices
           for field,choices in (("component",COMPONENTS),("event",EVENTS),("outcome",OUTCOMES))):
        return None,"invalid_event_contract"
    if not _time(row.get("start_unix")):return None,"missing_or_invalid_start_time"
    if not _duration(row.get("duration_ms")):return None,"missing_or_invalid_duration"
    # Invalid optional IDs cannot create a guessed grouping or leak raw values.
    # The event is still valid evidence, attached to the call root as needed.
    projected={"event_id":event_id,"config_id":row["config_id"],"call_key":key,
        "component":row["component"],"event":row["event"],"outcome":row["outcome"],
        "turn_id":_optional_identifier(row.get("turn_id"),_TURN),
        "request_id":_uuid(row.get("request_id")),
        "operation_id":_optional_identifier(row.get("operation_id"),_OPERATION),
        "start_unix":float(row["start_unix"]),"duration_ms":float(row["duration_ms"]),
        "attributes":safe_attributes(row.get("attributes"))}
    projected["end_unix"]=projected["start_unix"]+projected["duration_ms"]/1000
    return projected,None


def build_component_runs(snapshot,*,root_run_id,root_dotted_order,project_id):
    """Build append-only child payloads and honest coverage without network I/O.

    The caller supplies the actual call-review root and project IDs. Event IDs
    produce stable UUID5 children; retrying the same immutable snapshot produces
    identical payloads. Configuration is attached as recorded identity metadata,
    never invented as a timed execution span. A later scheduler receipt shares
    an operation group only when the recorded operation_id matches explicitly.
    A child may extend past call end: background work is never clipped to speech.
    """
    root=_uuid(root_run_id);project=_uuid(project_id)
    order=root_dotted_order.split(".") if type(root_dotted_order) is str else []
    if (root is None or root!=root_run_id or project is None or project!=project_id or not order
            or any(not _DOT_SEGMENT.fullmatch(part) for part in order) or not order[-1].endswith(root)):
        raise ValueError("invalid_component_span_destination")
    skips=Counter();duplicates=0
    raw=snapshot.get("events") if isinstance(snapshot,dict) else None
    key=snapshot.get("call_key") if isinstance(snapshot,dict) else None
    if not isinstance(key,str) or not _HASH.fullmatch(key):
        skips["missing_or_invalid_call_scope"]=len(raw) if isinstance(raw,list) else 1
        raw=[]
    if not isinstance(raw,list):skips["missing_event_list"]+=1;raw=[]
    if len(raw)>2000:skips["event_export_limit_exceeded"]+=len(raw)-2000
    unique={};conflicts=set()
    for row in raw[:2000]:
        value,reason=_project(row,key)
        if reason:skips[reason]+=1;continue
        eid=value["event_id"]
        if eid in unique:
            if unique[eid]!=value:conflicts.add(eid)
            else:duplicates+=1
        else:unique[eid]=value
    for eid in conflicts:unique.pop(eid);skips["conflicting_event_identity"]+=1
    events=sorted(unique.values(),key=lambda row:(row["start_unix"],row["event_id"]))
    groups={kind:defaultdict(list) for kind in ("turn","request","operation")}
    for row in events:
        for kind in groups:
            value=row[kind+"_id"]
            if value is not None:groups[kind][value].append(row)
    namespace=UUID(root)
    ids={(kind,key):str(uuid5(namespace,VERSION+":"+kind+":"+key)) for kind,values in groups.items() for key in values}
    parents={};payloads={};runs=[]
    def shared(rows,field):
        values={r[field] for r in rows}
        return next(iter(values)) if len(values)==1 and None not in values else None
    for turn in groups["turn"]:parents[("turn",turn)]=None
    for request,rows in groups["request"].items():
        turn=shared(rows,"turn_id")
        parents[("request",request)]=("turn",turn) if turn is not None else None
    for operation,rows in groups["operation"].items():
        request=shared(rows,"request_id");turn=shared(rows,"turn_id")
        parents[("operation",operation)]=("request",request) if request is not None else (("turn",turn) if turn is not None else None)
    def group(kind,key):
        identity=(kind,key)
        if identity in payloads:return payloads[identity]
        rows=groups[kind][key];parent=parents[identity]
        parent_payload=group(*parent) if parent is not None else None
        start=min(r["start_unix"] for r in rows);end=max(r["end_unix"] for r in rows)
        run_id=ids[identity]
        metadata={"record_kind":"component_group","span_schema_version":VERSION,"thread_id":root,
            "call_key":rows[0]["call_key"],kind+"_id":key,"configuration_ids":sorted({r["config_id"] for r in rows}),
            "grouping_only":True,"timing_basis":"observed_event_envelope_not_component_execution",
            "parentage_basis":"explicit_correlation_ids_not_causal_proof","event_count":len(rows),
            "provider_effective_configuration":"unknown"}
        if kind=="turn":metadata["turn_basis"]="recorded_turn_identifier_not_audio_turn_boundary"
        body={"id":run_id,"trace_id":root,"parent_run_id":parent_payload["id"] if parent_payload else root,
            "session_id":project,"name":"voice."+kind+".observed_events","run_type":"chain",
            "start_time":_iso(start),"end_time":_iso(end),
            "dotted_order":(parent_payload["dotted_order"] if parent_payload else root_dotted_order)+"."+_segment(start,run_id),
            "inputs":{"correlation_kind":kind,"correlation_id":key},
            "outputs":{"observed_event_count":len(rows),"complete_component_visibility":False},
            "tags":["component-evidence","grouping-only",VERSION],"extra":{"metadata":metadata}}
        payloads[identity]=body;runs.append(body)
        return body
    for kind in groups:
        for key in sorted(groups[kind]):group(kind,key)
    for row in events:
        parent=next(((kind,row[kind+"_id"]) for kind in ("operation","request","turn") if row[kind+"_id"] is not None),None)
        parent_payload=payloads[parent] if parent is not None else None
        run_id=str(uuid5(namespace,VERSION+":event:"+row["event_id"]))
        metadata={"record_kind":"component_event","span_schema_version":VERSION,"thread_id":root,
            "call_key":row["call_key"],"event_id":row["event_id"],"configuration_id":row["config_id"],
            "component":row["component"],"event":row["event"],"outcome":row["outcome"],
            "timing_basis":"recorded_event_start_and_duration","provider_effective_configuration":"unknown"}
        metadata.update({k:row[k] for k in ("turn_id","request_id","operation_id") if row[k] is not None})
        body={"id":run_id,"trace_id":root,"parent_run_id":parent_payload["id"] if parent_payload else root,
            "session_id":project,"name":"voice.component."+row["component"]+"."+row["event"],
            "run_type":"llm" if row["component"]=="llm" else ("tool" if row["component"] in {"tool_harness","scheduler"} else "chain"),
            "start_time":_iso(row["start_unix"]),"end_time":_iso(row["end_unix"]),
            "dotted_order":(parent_payload["dotted_order"] if parent_payload else root_dotted_order)+"."+_segment(row["start_unix"],run_id),
            "inputs":{"component":row["component"],"event":row["event"]},
            "outputs":{"outcome":row["outcome"],"measurements":row["attributes"]},
            "tags":["component-evidence",row["component"],VERSION],"extra":{"metadata":metadata}}
        if row["outcome"] in {"failure","failed","rejected"}:body["error"]="recorded_component_event_"+row["outcome"]
        runs.append(body)
    # Parents are always returned first. Equal starts still use a stable UUID order.
    return {"runs":runs,"coverage":{"schema_version":VERSION,"exported_event_spans":len(events),
        "exported_group_spans":len(runs)-len(events),"exact_duplicate_events_ignored":duplicates,
        "skipped_event_counts":dict(sorted(skips.items())),"complete_component_visibility":False,
        "root_time_containment_enforced":False,
        "interpretation":"Timed recorded events only; groups are derived correlation envelopes, not execution stages or causal proof. Missing-timing breadcrumbs remain in the call report; vendor/device audio remains unverified."}}
