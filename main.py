#!/usr/bin/env python3
"""iGraph CLI client for the four-module deployment.

The CLI no longer constructs an in-process Agent.  Backend remains the public
identity/policy boundary and Agent Runtime owns the conversation.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(override=False)
    backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:21109").rstrip("/")
    user_id = os.getenv("IGRAPH_CLI_USER_ID", "admin").strip()
    headers = {"X-User-ID": user_id}

    with httpx.Client(base_url=backend_url, headers=headers, timeout=1800, trust_env=False) as client:
        auth = client.post("/api/auth/validate")
        auth.raise_for_status()
        identity = auth.json()
        if not identity.get("valid"):
            raise SystemExit(f"用户校验失败: {identity.get('reason', 'unknown')}")

        created = client.post("/api/sessions", json={"model": identity.get("default_model")})
        created.raise_for_status()
        session_id = created.json()["session_id"]
        print(f"iGraph AgentOS CLI | user={user_id} | session={session_id}")
        print("输入 quit/exit 退出。")

        while True:
            try:
                message = input("\nYou > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not message:
                continue
            if message.lower() in {"quit", "exit", "q"}:
                break
            response = client.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": message},
            )
            response.raise_for_status()
            payload = response.json()
            print(f"\nAgent > {payload.get('response') or payload.get('error') or ''}")


if __name__ == "__main__":
    main()
