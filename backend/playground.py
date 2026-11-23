"""Account-scoped scenario conversations and idempotent example-project publication."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from backend.construction import sync_record
from backend.playground_context import planning_history
from construction_access import scope_headers
from execution_protocol import service_headers

log = logging.getLogger(__name__)
IN_PROGRESS = {"thinking", "building"}
CONFIRM = {"确认好问题了，开始构图", "确认问题，开始构图", "确认构图", "开始构图",
           "confirm and build", "build the example graph", "build the graph"}
LEASE_SECONDS = 210
# Unambiguous commands can commit the currently displayed proposal without another model round trip.
# Defer questions, quoted commands, and combined revision/build instructions to the dialogue planner.
DEFER_BUILD = re.compile(
    r"[?？“”「」`\"]|如果|假[如设]|例如|比如|确认[后时]|之后再|之前|等我|等到|等一下|稍后|明天|以后|晚点|不是现在|暂[时缓停]|"
    r"先别|先不|别.{0,5}(?:构|建|生成|画)|不(?:要|急|用|想|能|需要|打算).{0,8}(?:构|建|生成|画)|"
    r"还没|尚未|能否|是否|怎么|如何|为什么|可不可以|吗[。！\s]*$|"
    r"调整|修改|改成|改为|换成|换个|替换|增加|添加|删除|去掉|删掉|补充|"
    r"\b(?:if|when|once|after|before|until|wait|maybe|later|instead|unless|without|change|replace|revise|add|remove|why|how|whether|not)\b|n['’]t\b",
    re.I)


def defers_build(message):
    normalized = re.sub(r"(?:不用|无需|不必|不需要|不再)(?:再)?(?:调整|修改|改动|改|添加|补充)(?:问题)?(?:了)?", "", message)
    return bool(DEFER_BUILD.search(normalized))


class Turn(BaseModel):
    turn_id: uuid.UUID
    revision: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=4000)
    language: Literal["zh", "en"] = "zh"
    confirm: bool = False
    connection_revision: int = Field(ge=1)


def fail(code, status=409):
    raise HTTPException(status, {"code": code})


def empty(tid):
    return {"id": tid, "revision": 0, "status": "draft", "messages": [], "plan": None,
            "confirmable": False, "confirmed": False, "result": None, "live_reply": "", "error": None}


def public(doc):
    plan = doc["plan"]
    visible = {key: doc.get(key) for key in ("id", "revision", "status", "messages", "live_reply", "error",
                                             "confirmable", "confirmed", "result", "turn_id")}
    visible["plan"] = {key: plan[key] for key in ("title", "summary", "questions")} if plan else None
    return visible


class ScenarioStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS scenarios (owner TEXT, id TEXT, updated REAL, doc TEXT,"
                       " PRIMARY KEY(owner, id))")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _get(self, db, uid, tid):
        row = db.execute("SELECT doc FROM scenarios WHERE owner=? AND id=?", (uid, tid)).fetchone()
        if row is None:
            return None
        doc = json.loads(row[0])
        if doc["status"] in IN_PROGRESS and time.time() > doc.get("deadline", 0):
            doc.update(status="failed", confirmable=doc["confirmed"], live_reply="", error="PLAYGROUND_INTERRUPTED")
            self._save(db, uid, doc)
        return doc

    @staticmethod
    def _save(db, uid, doc):
        db.execute("INSERT OR REPLACE INTO scenarios VALUES(?,?,?,?)",
                   (uid, doc["id"], time.time(), json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))

    def list(self, uid):
        with self.transaction() as db:
            rows = db.execute("SELECT id,doc FROM scenarios WHERE owner=? ORDER BY updated DESC LIMIT 60", (uid,))
            return [{"id": tid, "title": (doc["plan"] or {}).get("title") or doc["messages"][0]["text"][:70]}
                    for tid, raw in rows if (doc := json.loads(raw))]

    def read(self, uid, tid):
        with self.transaction() as db:
            doc = self._get(db, uid, tid)
            if doc is None:
                fail("PLAYGROUND_NOT_FOUND", 404)
            return doc

    def begin(self, uid, tid, req):
        message = req.message.strip()
        if not message:
            fail("PLAYGROUND_EMPTY_MESSAGE", 422)
        with self.transaction() as db:
            doc = self._get(db, uid, tid) or empty(tid)
            if doc.get("turn_id") == str(req.turn_id):
                return doc, False
            if doc["revision"] != req.revision or doc["status"] in IN_PROGRESS or doc["result"]:
                fail("PLAYGROUND_CHANGED")
            # Free-form messages may revise the scope and ask to build in the same sentence.
            # Let the planner resolve those against the conversation before authorizing publication.
            explicit = req.confirm or message.casefold().strip("。.!！ ") in CONFIRM
            if (explicit or doc["confirmed"]) and not doc["confirmable"]:
                fail("PLAYGROUND_NEEDS_PLAN")
            if not (doc["status"] == "failed" and doc["messages"][-1]["text"] == message):
                doc["messages"].append({"role": "user", "text": message})
            if explicit:
                doc["confirmed"] = True
            doc.update(turn_id=str(req.turn_id), revision=doc["revision"] + 1,
                       status="building" if doc["confirmed"] else "thinking", deadline=time.time() + LEASE_SECONDS,
                       live_reply="", error=None)
            self._save(db, uid, doc)
            return doc, True

    def update(self, uid, doc, **changes):
        with self.transaction() as db:
            current = self._get(db, uid, doc["id"])
            if not current or current.get("turn_id") != doc["turn_id"] or current["status"] not in IN_PROGRESS:
                raise RuntimeError("Scenario turn no longer active")
            doc.update(changes)
            self._save(db, uid, doc)


def install_playground(app, *, path, graph, runtime, directory, projects, user_for, project_row, launch):
    store = ScenarioStore(path)

    def response(uid, doc):
        visible = public(doc)
        if doc["result"]:
            original = doc["result"]
            pid, did = original["project"]["project_id"], original["dataset_id"]
            project = projects.get(pid, uid)
            available = bool(project and not project.delete_status and directory.get(did, uid, pid))
            visible["result"] = {**original, "available": available}
            if available:
                visible["result"]["project"] = {**project_row(project), "dataset_count": len(directory.list(pid, uid))}
        return visible

    async def publish(uid, doc, analysis_model):
        plan = doc["plan"]
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"igraph:playground:{uid}:{doc['id']}"))
        did = str(uuid.uuid5(uuid.UUID(pid), "example-graph"))
        project = projects.stage_scenario(uid, pid, plan["title"], plan["summary"], analysis_model)
        if project.delete_status:
            fail("PROJECT_DELETING")
        source = {"format": "igraph.playground.v1", "data_origin": "synthetic", "scenario": plan, "model_name": doc["plan_model"]}
        dataset = directory.create(project_id=pid, user_id=uid, dataset_id=did, name=plan["title"],
            original_filename="playground-scenario.txt", content=json.dumps(source, ensure_ascii=False, indent=2).encode(),
            description=plan["summary"])
        state = await graph.request("POST", f"/construction/{did}/playground",
                                    headers={**service_headers(uid), **scope_headers(pid)}, timeout=45)
        if state.get("status") != "ready" or not state.get("node_count"):
            fail("PLAYGROUND_BUILD_FAILED", 502)
        sync_record(directory, dataset, state)
        project = projects.publish_scenario(pid, uid, did)
        return {"project": {**project_row(project), "dataset_count": len(directory.list(pid, uid))}, "dataset_id": did,
                "node_count": state["node_count"], "edge_count": state["edge_count"]}

    async def run(uid, doc, req, analysis_model):
        try:
            if not doc["confirmed"]:
                payload = {"connection_revision": req.connection_revision, "language": req.language, "message": req.message.strip(),
                           "previous": doc["plan"], "confirmable": doc["confirmable"] and not defers_build(req.message),
                           "notes": doc.get("notes", []),
                           "history": planning_history(doc["messages"], req.language, bootstrap=not doc.get("notes"))}
                decision = model_name = None
                notes = doc.get("notes", [])
                last = 0.0
                async with asyncio.timeout(175), graph.stream("POST", "/internal/playground/plan",
                        headers=service_headers(uid), json_body=payload) as stream:
                    async for line in stream.aiter_lines():
                        if not line:
                            continue
                        event = json.loads(line)
                        if event.get("error"):
                            fail(event["error"], 502)
                        if event.get("reply") and time.monotonic() - last > 0.16:
                            store.update(uid, doc, live_reply=event["reply"])
                            last = time.monotonic()
                        if event.get("decision"):
                            decision = event["decision"]
                            model_name = event["model_name"]
                            notes = event.get("notes", notes)
                if not decision:
                    fail("PLAYGROUND_INVALID_REPLY", 502)
                action = decision["action"]
                response = {"role": "assistant", "text": decision["reply"]}
                if action == "propose":
                    plan = decision["plan"]
                    response["questions"] = plan["questions"]
                    store.update(uid, doc, plan=plan, plan_model=model_name, notes=notes, confirmable=True, live_reply="",
                                 status="draft", messages=[*doc["messages"], response])
                    return
                if action == "clarify":
                    store.update(uid, doc, notes=notes, confirmable=False, live_reply="", status="draft",
                                 messages=[*doc["messages"], response])
                    return
                if action != "confirm" or not doc["confirmable"] or not doc["plan"]:
                    fail("PLAYGROUND_NEEDS_PLAN")
                store.update(uid, doc, confirmed=True, status="building", deadline=time.time() + LEASE_SECONDS, live_reply="",
                             messages=[*doc["messages"], response])
            result = await publish(uid, doc, analysis_model)
            store.update(uid, doc, result=result, status="ready", confirmable=False, live_reply="")
        except Exception as exc:
            code = "PLAYGROUND_BUILD_FAILED" if doc["confirmed"] else "PLAYGROUND_MODEL_FAILED"
            if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                code = exc.detail.get("code") or code
            if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                code = "PLAYGROUND_TIMEOUT"
            log.warning("Playground turn failed turn=%s code=%s error=%s", doc["turn_id"], code, type(exc).__name__)
            store.update(uid, doc, status="failed", confirmable=doc["confirmed"], live_reply="", error=code)

    @app.get("/api/playground")
    def list_scenarios(request: Request):
        return {"scenarios": store.list(user_for(request).user_id)}

    @app.get("/api/playground/connection")
    async def read_connection(request: Request):
        return await runtime.request("GET", "/internal/playground/connection", headers=service_headers(user_for(request).user_id))

    @app.put("/api/playground/connection")
    async def save_connection(request: Request, req: dict):
        return await runtime.request("PUT", "/internal/playground/connection", headers=service_headers(user_for(request).user_id),
                                     json_body=req)

    @app.delete("/api/playground/connection")
    async def remove_connection(revision: int, request: Request):
        return await runtime.request("DELETE", "/internal/playground/connection", params={"revision": revision},
                                     headers=service_headers(user_for(request).user_id))

    @app.get("/api/playground/{scenario_id}")
    def get_scenario(scenario_id: uuid.UUID, request: Request):
        uid = user_for(request).user_id
        return response(uid, store.read(uid, str(scenario_id)))

    @app.post("/api/playground/{scenario_id}/turns", status_code=202)
    async def submit(scenario_id: uuid.UUID, req: Turn, request: Request):
        uid = user_for(request).user_id
        settings = await runtime.request("GET", "/internal/playground/connection", headers=service_headers(uid))
        if not settings["configured"]:
            fail("PLAYGROUND_NOT_CONFIGURED")
        if settings["revision"] != req.connection_revision:
            fail("MODEL_CONFIG_CHANGED")
        doc, started = store.begin(uid, str(scenario_id), req)
        if started:
            launch(run(uid, doc, req, settings['config_id']))
        return response(uid, doc)
