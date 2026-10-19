"""Explicitly scripted, credential-free rehearsal; not a language model."""
import re
from datetime import date,timedelta
from .domain import SERVICES,today,uid
from .tools import execute

ALIASES={"oil_change":("oil","filter"),"tires":("tire","tyre","rotation"),"brakes":("brake",),"diagnostic":("diagnostic","check engine","engine light")}

def parse_date(text):
    low=text.lower()
    match=re.search(r"\b\d{4}-\d{2}-\d{2}\b",low)
    if match:
        try:
            return date.fromisoformat(match[0]).isoformat()
        except ValueError:
            return None
    if "tomorrow" in low:
        return (today()+timedelta(days=1)).isoformat()
    if "today" in low:
        return today().isoformat()
    for i,day in enumerate(("monday","tuesday","wednesday","thursday","friday","saturday","sunday")):
        if day in low:
            diff=(i-today().weekday())%7
            return (today()+timedelta(days=diff or 7)).isoformat()
    return None

def respond(store,sid,text):
    low=text.lower().strip(); state=store.state(sid)
    def run(name,args=None):
        return execute(store,sid,name,args or {},uid("rehearsal"))
    def offer(day=None):
        r=run("search_availability",{"service":state["service"],"date":day})
        if not r["ok"]: return r["message"]
        if not r["slots"]: return r["message"]+" Try Monday, another date, or ‘next available’."
        choices=" ".join(f"{i+1}. {s['label']}." for i,s in enumerate(r["slots"]))
        return "Here are the current openings. "+choices+" Choose 1, 2, or 3."
    if any(x in low for x in ("human","advisor","person","recall","warranty","loaner","smoke","fire","unsafe","brakes failed")):
        r=run("request_handoff",{"reason":text[:500]})
        return r["message"]
    if any(x in low for x in ("hours","open","price","cost","address","policy","policies")) and not any(x in low for x in ("opening","openings")):
        r=run("get_dealership_info")
        if "hour" in low or "open" in low: return r["hours"]+" What service would you like to arrange?"
        return r["policies"]
    if "reschedul" in low or low in ("change appointment","move appointment"):
        appt=run("get_appointment")["appointment"]
        if not appt or appt["status"]!="confirmed": return "There’s no confirmed appointment in this session. Say a service to begin, or ‘advisor’ for another appointment."
        store.invalidate(sid);store.update(sid,{"action":"reschedule","service":appt["service"],"stage":"date"})
        return "What day would you like to move it to? Your original appointment stays booked until the change succeeds."
    if low in ("cancel","cancel appointment","cancel my appointment"):
        r=run("prepare_cancellation")
        return r["summary"]+" Type ‘confirm’ to cancel, or ‘no’ to keep it." if r["ok"] else r["message"]
    if low in ("status","my appointment","booking status"):
        appt=run("get_appointment")["appointment"]
        return f"{appt['reference']} is {appt['status']}: {appt['service_name']} on {appt['label']}." if appt else "No appointment has been saved in this session."
    if state["stage"]=="confirmation":
        if low in ("confirm","yes","yes please","book it","yes book it"):
            r=run("confirm_action",{"proposal_id":state["proposal_id"],"confirmed":True})
            if not r["ok"]: return r["message"]+" Choose ‘next available’ to check again."
            appt=r["appointment"]
            return f"{appt['reference']} is {appt['status']}. {appt['service_name']} for {appt['customer_name']}, {appt['vehicle']}, on {appt['label']}. "+("You can say ‘reschedule’, ‘cancel’, or ‘advisor’." if appt["status"]=="confirmed" else "Nothing else is booked.")
        if low in ("no","no thanks","don't","stop","change"):
            store.invalidate(sid);store.update(sid,{"stage":"date"})
            return "I haven’t made that change. What date should I check instead?"
        # Corrections cannot inherit confirmation. Accept a date, service or customer detail below.
        store.invalidate(sid)
        if "," in text:
            store.update(sid,{"stage":"details"});state=store.state(sid)
        else:
            store.update(sid,{"stage":"date"});state=store.state(sid)
    service=next((s for s,terms in ALIASES.items() if any(t in low for t in terms)),None)
    if service and state["stage"]!="details":
        store.invalidate(sid);store.update(sid,{"service":service,"stage":"date","action":"book"});state=store.state(sid)
        day=parse_date(text)
        if day: return offer(day)
        return f"I can help with {SERVICES[service]['name'].lower()}. Which day works? Try ‘Monday’, YYYY-MM-DD, or ‘next available’."
    if low in ("next available","soonest","earliest") and state.get("service"):
        return offer()
    day=parse_date(text)
    if day and state.get("service") and state["stage"]!="details":
        return offer(day)
    if state["stage"]=="slot":
        choice=re.fullmatch(r"(?:option )?([123])",low)
        if not choice: return "Choose 1, 2, or 3 from the offered times, or give another date."
        index=int(choice[1])-1
        if index>=len(state["slots"]): return "That option is not available. Choose one of the listed times."
        slot=state["slots"][index]
        store.update(sid,{"selected_slot":slot["id"]})
        if state.get("action")=="reschedule":
            r=run("prepare_reschedule",{"slot_id":slot["id"]})
            return r["summary"]+" Type ‘confirm’ to move it, or ‘no’." if r["ok"] else r["message"]
        store.update(sid,{"stage":"details"})
        return "What are your name, callback number, and vehicle? For this rehearsal use: Alex Morgan, 5125550142, 2021 Toyota Camry."
    if state["stage"]=="details":
        parts=[p.strip() for p in text.split(",",2)]
        if len(parts)!=3: return "This scripted rehearsal expects three comma-separated details: Alex Morgan, 5125550142, 2021 Toyota Camry. Live Vapi voice can collect them conversationally."
        r=run("prepare_booking",{"service":state["service"],"slot_id":state["selected_slot"],"customer_name":parts[0],"phone":parts[1],"vehicle":parts[2]})
        return r["summary"]+" Shall I book this? Type ‘confirm’ or ‘no’." if r["ok"] else r["message"]
    if state["stage"]=="complete":
        return "You can check ‘status’, ‘reschedule’, ‘cancel’, or ask for an ‘advisor’. Start a new session for another customer."
    if state["stage"]=="handoff":
        return "Your request is in the local demo advisor queue. This prototype does not deliver callbacks. Start a new session to rehearse another call."
    return "This is a scripted rehearsal. Try ‘oil change’, ‘tires’, ‘brakes’, ‘diagnostic’, ‘hours’, or ‘advisor’."
