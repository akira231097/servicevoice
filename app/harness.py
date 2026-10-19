"""A bounded, evidence-preserving tool runner for a live conversation.

This layer uses no LLM. An HTTP deadline never cancels an accepted effect.
Every successful receipt points at a result committed by the domain transaction.
Unresolved work from another process remains UNKNOWN rather than being repeated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .tools import DEFINITIONS, execute

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)

def result_receipt(result,operation_id):
    raw={k:v for k,v in result.items() if k!="_receipt"}
    return {**raw,"_receipt":{"operation_id":operation_id,"result_sha256":hashlib.sha256(canonical(raw).encode()).hexdigest()}}

def operation_error(code,operation_id,message):
    return {"ok":False,"error":code,"operation_id":operation_id,"message":message}

class ToolHarness:
    def __init__(self,store,timeout_seconds=1.5,max_concurrency=4,max_pending=64,max_registrars=4,
                 status_timeout_seconds=0.5,max_status_reads=4):
        if (timeout_seconds<=0 or max_concurrency<1 or not isinstance(max_pending,int) or max_pending<1
                or type(max_registrars) is not int or max_registrars<1):
            raise ValueError("Tool deadline, concurrency, pending and registrar capacities must be positive.")
        if (type(status_timeout_seconds) not in (int,float) or not math.isfinite(status_timeout_seconds)
                or status_timeout_seconds<=0 or type(max_status_reads) is not int or max_status_reads<1):
            raise ValueError("Status deadline and read capacity must be positive and finite.")
        self.store=store
        self.timeout_seconds=timeout_seconds
        self.owner=uuid.uuid4().hex
        self.semaphore=asyncio.Semaphore(max_concurrency)
        # Bound registration separately from effects and the total backlog.
        # The measured reference uses four registrars with SQLite controlling
        # writer exclusion; one remains available for controlled comparisons.
        self.admission_lock=asyncio.Lock() if max_registrars==1 else asyncio.Semaphore(max_registrars)
        self.max_pending=max_pending
        self.inflight={}
        self.closing=False
        self.status_timeout_seconds=status_timeout_seconds
        self.status_busy_timeout_seconds=min(0.2,status_timeout_seconds/2)
        self.max_status_reads=max_status_reads
        self.status_reads={}
        # Set by application startup; evaluation fixtures stay network-free.
        self.telemetry=None

    def _receipt_for(self,db,sid,call_id,opid):
        row=db.execute("SELECT result FROM tool_results WHERE session_id=? AND call_id=?",(sid,call_id)).fetchone()
        return result_receipt(json.loads(row[0]),opid) if row else None

    def _register(self,sid,name,arguments,call_id,opid,fingerprint):
        with self.store.transaction() as db:
            row=db.execute("SELECT * FROM tool_invocations WHERE id=? AND session_id=?",(opid,sid)).fetchone()
            if row and row["fingerprint"]!=fingerprint:
                return False,operation_error("idempotency_conflict",opid,"The retry changed this request. Check its original operation result.")
            # The domain transaction's result is authoritative even after a process died before journal completion.
            cached=db.execute("SELECT * FROM tool_results WHERE session_id=? AND call_id=?",(sid,call_id)).fetchone()
            if cached:
                # Store.dispatch used the older JSON serialization for the same normalized input.
                domain_fp=hashlib.sha256(json.dumps({"name":name,"args":arguments},sort_keys=True).encode()).hexdigest()
                if cached["fingerprint"]!=domain_fp:
                    return False,operation_error("idempotency_conflict",opid,"The retry changed this request.")
                timestamp=time.time()
                db.execute("INSERT OR IGNORE INTO tool_invocations VALUES (?,?,?,?,?,?,?,?,?)",(opid,sid,call_id,fingerprint,self.owner,"completed" if json.loads(cached["result"]).get("ok") else "rejected",timestamp,timestamp,None))
                return False,result_receipt(json.loads(cached["result"]),opid)
            if row:
                return False,self._unresolved(row,opid)
            session=db.execute("SELECT ended,created FROM sessions WHERE id=?",(sid,)).fetchone()
            if not session:
                return False,operation_error("unauthorized",opid,"Unknown session.")
            if session["ended"] or time.time()-session["created"]>4*3600:
                return False,operation_error("session_ended",opid,"This session cannot start a new action.")
            timestamp=time.time()
            db.execute("INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?,?)",(opid,sid,call_id,fingerprint,self.owner,"queued",timestamp,timestamp,None))
            return True,None

    def _mark(self,sid,opid,status,detail=None):
        with self.store.transaction() as db:
            db.execute("UPDATE tool_invocations SET status=?,updated=?,detail=? WHERE id=? AND session_id=? AND owner=?",
                       (status,time.time(),detail,opid,sid,self.owner))

    def _unresolved(self,row,opid):
        if row["status"]=="failed":
            return operation_error("operation_failed",opid,"The tool did not produce an authoritative result. Check the appointment or request an advisor; do not claim completion.")
        active=any(key[0]==row["session_id"] and task_opid==opid and not task.done() for key,(fp,task_opid,task) in list(self.inflight.items()))
        if row["owner"]==self.owner and active and row["status"] in ("queued","running"):
            return operation_error("operation_pending",opid,"The original action is still running. Do not repeat it or claim success. Check get_operation with this operation_id.")
        return operation_error("operation_unknown",opid,"No authoritative result is available after an interrupted operation. Reconcile with the scheduler or an advisor; do not submit a new write.")

    def _lookup_connection(self,db,sid,opid):
        row=db.execute("SELECT * FROM tool_invocations WHERE id=? AND session_id=?",(opid,sid)).fetchone()
        if not row:
            if any(key[0]==sid and task_opid==opid and not task.done() for key,(fp,task_opid,task) in list(self.inflight.items())):
                return operation_error("operation_pending",opid,"The original request is waiting for durable admission. Do not repeat it; check this operation again.")
            return operation_error("operation_missing",opid,"No operation with that ID exists in this session.")
        result=self._receipt_for(db,sid,row["call_id"],opid)
        return result if result is not None else self._unresolved(row,opid)

    def _lookup(self,sid,opid):
        # Internal commit verification keeps its original connection policy.
        with self.store.connect() as db:
            return self._lookup_connection(db,sid,opid)

    def _lookup_status(self,sid,opid):
        with self.store.read_connection(self.status_busy_timeout_seconds) as db:
            return self._lookup_connection(db,sid,opid)

    def _status_unavailable(self,opid,reason):
        return {**operation_error("status_unavailable",opid,
            "The operation status could not be verified right now; the action may already have completed. Check get_operation with this same operation_id. Never repeat a write based on this unavailable status."),
            "reason":reason,"retry_after_ms":250}

    async def _read_status(self,sid,opid):
        try:
            return await run_in_threadpool(self._lookup_status,sid,opid)
        except Exception:
            # A failed read establishes nothing about the original action.
            return self._status_unavailable(opid,"read_error")

    async def operation(self,sid,operation_id):
        key=(sid,operation_id)
        task=self.status_reads.get(key)
        if task is None:
            if sum(not read.done() for read in list(self.status_reads.values()))>=self.max_status_reads:
                return self._status_unavailable(operation_id,"capacity")
            task=asyncio.create_task(self._read_status(sid,operation_id))
            self.status_reads[key]=task
            def finished(done):
                if self.status_reads.get(key) is done:self.status_reads.pop(key,None)
            task.add_done_callback(finished)
        try:
            # Keep a timed-out/canceled reader counted until its worker actually
            # completes; otherwise repeated polling could exhaust the threadpool.
            return await asyncio.wait_for(asyncio.shield(task),timeout=self.status_timeout_seconds)
        except asyncio.TimeoutError:
            return self._status_unavailable(operation_id,"deadline")

    def _execute_registered(self,sid,name,arguments,call_id,opid):
        """One worker hop, with separate durable boundaries around the effect.

        Each DB transaction remains short. Slow adapter I/O must not hold an
        open transaction or bypass the authoritative committed-result check.
        """
        self._mark(sid,opid,"running")
        execute(self.store,sid,name,arguments,call_id)
        # The worker's return alone cannot create a receipt: read committed data.
        authoritative=self._lookup(sid,opid)
        if "_receipt" not in authoritative:
            self._mark(sid,opid,"failed","missing_authoritative_result")
            return operation_error("operation_failed",opid,"No committed tool receipt exists. Do not claim the action completed.")
        self._mark(sid,opid,"completed" if authoritative.get("ok") else "rejected")
        return authoritative

    async def _work(self,sid,name,arguments,call_id,opid,fingerprint):
        try:
            async with self.admission_lock:
                is_new,result=await run_in_threadpool(self._register,sid,name,arguments,call_id,opid,fingerprint)
            if not is_new:return result
            async with self.semaphore:
                return await run_in_threadpool(self._execute_registered,sid,name,arguments,call_id,opid)
        except asyncio.CancelledError:
            # Cancellation cannot prove an upstream or worker-thread action was undone.
            await run_in_threadpool(self._mark,sid,opid,"unknown","worker_cancelled")
            raise
        except Exception:
            # Read committed evidence before classifying an exception after a potential commit.
            try:
                authoritative=await self.operation(sid,opid)
                if "_receipt" in authoritative:return authoritative
                if authoritative.get("error")=="status_unavailable":
                    return operation_error("operation_unknown",opid,
                        "The tool raised an error and its committed outcome could not be read. Check this same operation again or request an advisor; do not repeat the write or infer failure.")
                await run_in_threadpool(self._mark,sid,opid,"failed","execution_exception")
            except Exception:
                return operation_error("operation_unknown",opid,"The operation status could not be read. No outcome can be confirmed. Request an advisor.")
            return operation_error("operation_failed",opid,"The tool did not return a verified result. Check the appointment or request an advisor.")

    async def run(self,sid,name,arguments,call_id):
        """Observe the caller's wait without changing accepted-effect semantics."""
        started=time.perf_counter()
        start_unix=time.time()
        result=None
        try:
            result=await self._run(sid,name,arguments,call_id)
            return result
        finally:
            # No arguments, result bodies, caller identifiers or exceptions are
            # given to the trace exporter. It may never change the tool result.
            try:
                if self.telemetry is not None:
                    self.telemetry.record_event("tool.execution",{
                        "tool_name":name if isinstance(name,str) and name in DEFINITIONS else "unknown_tool",
                        "ok":result.get("ok") is True if isinstance(result,dict) else None,
                        "error_code":result.get("error") if isinstance(result,dict) else "waiter_interrupted",
                        "receipt_present":isinstance(result,dict) and "_receipt" in result,
                        "admitted":result.get("admitted") if isinstance(result,dict) else None,
                    },correlation=sid,start_unix=start_unix,
                        duration_ms=(time.perf_counter()-started)*1000)
            except Exception:
                pass

    async def _run(self,sid,name,arguments,call_id):
        if not isinstance(name,str) or name not in DEFINITIONS:
            return {"ok":False,"error":"unknown_tool","message":"Unsupported tool."}
        if not isinstance(call_id,str) or not 1<=len(call_id)<=200:
            return {"ok":False,"error":"invalid_arguments","message":"A stable provider call ID is required."}
        try:
            normalized=DEFINITIONS[name][0].model_validate(arguments).model_dump()
        except ValidationError:
            return {"ok":False,"error":"invalid_arguments","message":"Arguments do not match the strict schema. Ask for missing details instead of guessing."}
        if name=="get_operation":
            return await self.operation(sid,normalized["operation_id"])
        opid="op_"+hashlib.sha256((sid+"\0"+call_id).encode()).hexdigest()[:24]
        fingerprint=hashlib.sha256(canonical({"name":name,"args":normalized}).encode()).hexdigest()
        key=(sid,call_id)
        current=self.inflight.get(key)
        if current:
            if current[0]!=fingerprint:
                return operation_error("idempotency_conflict",opid,"The retry changed the request.")
            task=current[2]
        else:
            if self.closing:
                return {**operation_error("harness_closed",opid,
                    "This attempt was not admitted because the service is shutting down. Check any earlier operation with get_operation before retrying the same call ID after service resumes."),
                    "admitted":False}
            if sum(not pending.done() for _,_,pending in list(self.inflight.values()))>=self.max_pending:
                return {**operation_error("harness_overloaded",opid,
                    "This attempt was not admitted and did not start an action. Retry the same call ID when capacity is available; if it was submitted earlier, check get_operation first."),
                    "admitted":False}
            task=asyncio.create_task(self._work(sid,name,normalized,call_id,opid,fingerprint))
            self.inflight[key]=(fingerprint,opid,task)
            def finished(done):
                if self.inflight.get(key,(None,None,None))[2] is done:self.inflight.pop(key,None)
            task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task),timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return operation_error("operation_pending",opid,"The original action is still processing. Do not repeat it or claim success; check get_operation with this operation_id.")

    async def drain(self):
        tasks=[task for _,_,task in list(self.inflight.values())]
        if tasks:await asyncio.gather(*(asyncio.shield(task) for task in tasks),return_exceptions=True)

    async def close(self):
        """Stop new admission before waiting; existing effects remain shielded.

        The flag is set before the first await, so requests on this event loop
        cannot slip into the draining snapshot. Reads of existing operations
        and exact in-flight duplicate waiters remain available during shutdown.
        """
        self.closing=True
        await self.drain()
