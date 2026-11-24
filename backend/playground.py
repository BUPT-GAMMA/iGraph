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
APPLY_SCHEMA = {"确认", "确认修改", "确认应用", "应用修改", "应用这些修改", "确认应用修改", "按这个方案修改",
                "confirm", "apply", "apply changes", "apply the changes", "confirm changes", "yes, apply the changes"}
CANCEL_SCHEMA = {"取消修改", "取消这次修改", "放弃修改", "cancel changes", "discard changes"}
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
BUILD_INTENT = re.compile(
    r"(?:开始|直接|现在|请|可以|就|照此|帮我|按.{1,12}).{0,18}"
    r"(?:(?:构建|创建|生成|绘制|搭建|画|建|构).{0,7}图|图.{0,5}(?:建|构|生成|画))|"
    r"^\s*(?:构图|建图|生成示例图|开建)(?:吧|好了|即可|就行)?[！!。\s]*$|"
    r"^(?:就|请)?按(?:这个|当前|这版)(?:方案|版本)?(?:开始|生成|建起来)(?:吧|了)?[！!。\s]*$|"
    r"(?:没问题|确认|定了|可以了).{0,10}(?:开始|生成|建起来)(?:吧|了|[！!。\s]*$)|"
    r"\b(?:build|generate|create|construct|draw)\s+(?:(?:the|this|that|a|an|sample|example|demo|scenario)\s+)*(?:graph|network|it)\b|"
    r"\b(?:go ahead|proceed)\s+(?:and|with)\s+(?:the\s+)?(?:build|construction|graph)\b|"
    r"^(?:(?:looks good|approved|yes)[,.!\s]+)?(?:go ahead|proceed)(?: now)?[.!\s]*$",
    re.I)
APPROVAL_CLAUSE = re.compile(
    r"(?:the |these |those )?(?:questions|proposal|plan)(?: are| is)? (?:confirmed|approved)|"
    r"looks good|approved|yes|okay|ok|没问题|可以了|"
    r"(?:这些|这[一二两三四五六1-6]道)?(?:问题|题|方案)(?:就)?(?:确认(?:好了|好|了)?|定了)|"
    r"(?:就)?按(?:屏幕上|刚才|当前)?(?:这|那|这个|这版)?(?:[一二两三四五六1-6]道)?(?:问题|题|方案|版本)", re.I)
PUBLISH_CLAUSE = re.compile(
    r"(?:please )?(?:go ahead and )?(?:build|generate|create|construct|draw) "
    r"(?:(?:the|this|that|a|an|sample|example|demo|scenario) )*(?:graph|network|it)(?: now)?|"
    r"(?:请|现在|直接|帮我|可以)*(?:(?:开始)?(?:构图|建图)|(?:生成|构建|创建)(?:这个|这张)?(?:示例图|样例图|图)|"
    r"(?:把)?(?:这个|这张)?(?:示例图|样例图|图)(?:建出来|建起来|生成出来))(?:吧|即可|就行)?", re.I)


def defers_build(message):
    normalized = re.sub(r"(?:不用|无需|不必|不需要|不再)(?:再)?(?:调整|修改|改动|改|添加|补充)(?:问题)?(?:了)?", "", message)
    return bool(DEFER_BUILD.search(normalized))


def permits_build(message):
    return not defers_build(message) and bool(BUILD_INTENT.search(message))


def approves_without_edits(message):
    clauses = [part.strip() for part in re.split(r"[，,。.!！;；]+", message) if part.strip()]
    return bool(clauses and PUBLISH_CLAUSE.fullmatch(clauses[-1])
                and all(APPROVAL_CLAUSE.fullmatch(part) for part in clauses[:-1]))


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
            doc.update(status="failed", confirmable=bool(doc.get("pending_schema") or doc["confirmed"]),
                       live_reply="", error="PLAYGROUND_INTERRUPTED")
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
            if doc["revision"] != req.revision or doc["status"] in IN_PROGRESS:
                fail("PLAYGROUND_CHANGED")
            # Free-form messages may revise the scope and ask to build in the same sentence.
            # Let the planner resolve those against the conversation before authorizing publication.
            command = message.casefold().strip("。.!！ ")
            if doc["result"]:
                doc["result"].setdefault("after_message", len(doc["messages"]))
                apply = req.confirm or command in APPLY_SCHEMA and bool(doc.get("pending_schema"))
                if apply and not (doc.get("pending_schema") and doc["confirmable"]):
                    fail("PLAYGROUND_NO_SCHEMA_EDITS")
                doc["confirmed"] = apply
            else:
                explicit = req.confirm or command in CONFIRM or doc["confirmable"] and approves_without_edits(message)
                if (explicit or doc["confirmed"]) and not doc["confirmable"]:
                    fail("PLAYGROUND_NEEDS_PLAN")
                if explicit:
                    doc["confirmed"] = True
            if not (doc["status"] == "failed" and doc["messages"][-1]["text"] == message):
                doc["messages"].append({"role": "user", "text": message})
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

    async def response(uid, doc, *, refresh_schema=False):
        visible = public(doc)
        if doc["result"]:
            original = doc["result"]
            pid, did = original["project"]["project_id"], original["dataset_id"]
            project = projects.get(pid, uid)
            available = bool(project and not project.delete_status and directory.get(did, uid, pid))
            visible["result"] = {**original, "available": available}
            if available:
                visible["result"]["project"] = {**project_row(project), "dataset_count": len(directory.list(pid, uid))}
                if refresh_schema and doc["status"] not in IN_PROGRESS:
                    try:
                        schema = await graph.request("GET", f"/construction/{did}/playground/schema",
                                                     headers={**service_headers(uid), **scope_headers(pid)})
                        visible["result"].update(schema=schema, node_count=schema["node_count"], edge_count=schema["edge_count"])
                    except HTTPException as exc:
                        if exc.status_code in {403, 404}:
                            visible["result"]["available"] = False
                        else:
                            visible["result"]["schema_unavailable"] = True
                    except httpx.HTTPError:
                        visible["result"]["schema_unavailable"] = True
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
                "node_count": state["node_count"], "edge_count": state["edge_count"], "schema": state.get("schema"),
                "after_message": len(doc["messages"])}

    async def edit_schema(uid, doc, req):
        result = doc["result"]
        pid, did = result["project"]["project_id"], result["dataset_id"]
        project, dataset = projects.get(pid, uid), directory.get(did, uid, pid)
        if not project or project.delete_status or not dataset:
            fail("PLAYGROUND_GRAPH_UNAVAILABLE", 404)
        path = f"/construction/{did}/playground/schema"
        headers = {**service_headers(uid), **scope_headers(pid)}
        pending = doc.get("pending_schema")
        if doc["confirmed"]:
            state = await graph.request("POST", path + "/apply", headers=headers, json_body=pending, timeout=40)
            sync_record(directory, dataset, state)
            schema = state["schema"]
            pending = None
            message = {"role": "assistant", "kind": "schema_applied", "text": (
                "The changes are saved to this graph." if req.language == "en" else "类型修改已保存到这个项目的图中。")}
        elif pending and req.message.strip().casefold().strip("。.!！ ") in CANCEL_SCHEMA:
            schema = await graph.request("GET", path, headers=headers)
            pending = None
            message = {"role": "assistant", "text": "Changes discarded." if req.language == "en" else "已取消这次类型修改。"}
        else:
            payload = {"connection_revision": req.connection_revision, "language": req.language, "message": req.message.strip(),
                       "scenario": (doc.get("plan") or {}).get("summary", ""), "notes": doc.get("notes", []), "pending": pending,
                       "history": planning_history(doc["messages"], req.language, bootstrap=False)}
            decision = await graph.request("POST", path + "/plan", headers=headers, json_body=payload, timeout=165)
            schema = decision["schema"]
            message = {"role": "assistant", "text": decision["reply"]}
            if decision["action"] == "propose":
                pending = {**decision["edits"], "base_revision": schema["revision"], "proposal_id": doc["turn_id"]}
                message.update(preview=decision["preview"], changes=pending["changes"],
                               before_counts={"node_count": schema["node_count"], "edge_count": schema["edge_count"]})
            elif decision["action"] == "cancel":
                pending = None
        store.update(uid, doc, status="ready", confirmed=False, confirmable=bool(pending), pending_schema=pending, live_reply="",
                     messages=[*doc["messages"], message],
                     result={**result, "schema": schema, "node_count": schema["node_count"], "edge_count": schema["edge_count"],
                             "after_message": len(doc["messages"]) + 1 if doc["confirmed"] else result["after_message"]})

    async def run(uid, doc, req, analysis_model):
        try:
            if doc["result"]:
                await edit_schema(uid, doc, req)
                return
            if not doc["confirmed"]:
                can_confirm = doc["confirmable"] and permits_build(req.message)
                payload = {"connection_revision": req.connection_revision, "language": req.language, "message": req.message.strip(),
                           "previous": doc["plan"], "confirmable": can_confirm,
                           "notes": doc.get("notes", []), "tools_reviewed": doc.get("tools_reviewed", False),
                           "history": planning_history(doc["messages"], req.language, bootstrap=not doc.get("notes"))}
                decision = model_name = None
                notes = doc.get("notes", [])
                reviewed = False
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
                            reviewed = event.get("tools_reviewed", False)
                if not decision:
                    fail("PLAYGROUND_INVALID_REPLY", 502)
                action = decision["action"]
                response = {"role": "assistant", "text": decision["reply"]}
                if action == "propose":
                    plan = decision["plan"]
                    response["questions"] = plan["questions"]
                    store.update(uid, doc, plan=plan, plan_model=model_name, notes=notes, tools_reviewed=reviewed,
                                 confirmable=True, live_reply="",
                                 status="draft", messages=[*doc["messages"], response])
                    return
                if action == "clarify":
                    store.update(uid, doc, notes=notes, confirmable=False, live_reply="", status="draft",
                                 messages=[*doc["messages"], response])
                    return
                if action != "confirm" or not can_confirm or not doc["plan"]:
                    fail("PLAYGROUND_NEEDS_PLAN")
                store.update(uid, doc, confirmed=True, status="building", deadline=time.time() + LEASE_SECONDS, live_reply="",
                             messages=[*doc["messages"], response])
            result = await publish(uid, doc, analysis_model)
            store.update(uid, doc, result=result, status="ready", confirmed=False, confirmable=False, live_reply="")
        except Exception as exc:
            code = "PLAYGROUND_BUILD_FAILED" if doc["confirmed"] else "PLAYGROUND_MODEL_FAILED"
            if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                code = exc.detail.get("code") or code
            if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                code = "PLAYGROUND_TIMEOUT"
            log.warning("Playground turn failed turn=%s code=%s error=%s", doc["turn_id"], code, type(exc).__name__)
            if code in {"PLAYGROUND_SCHEMA_CHANGED", "PLAYGROUND_GRAPH_UNAVAILABLE"} and doc["result"]:
                store.update(uid, doc, status="ready", pending_schema=None, confirmed=False, confirmable=False, live_reply="", error=code)
            else:
                store.update(uid, doc, status="failed", confirmable=bool(doc.get("pending_schema") or doc["confirmed"]),
                             live_reply="", error=code)

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
    async def get_scenario(scenario_id: uuid.UUID, request: Request):
        uid = user_for(request).user_id
        return await response(uid, store.read(uid, str(scenario_id)), refresh_schema=True)

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
        return await response(uid, doc)
