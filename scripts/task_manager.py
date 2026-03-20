#!/usr/bin/env python3
"""
task_manager.py — Taco's Multi-Agent Task Queue
Manages the task queue for the agent team system.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

QUEUE_DIR = Path.home() / ".openclaw/workspace/trading/task_queue"
TELEGRAM_TOKEN = "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4"
CHAT_ID = "7520899464"


def _load_tasks(status):
    dir_path = QUEUE_DIR / status
    tasks = []
    if dir_path.exists():
        for f in sorted(dir_path.glob("*.json")):
            try:
                with open(f) as fp:
                    tasks.append(json.load(fp))
            except Exception:
                pass
    return tasks


def list_tasks(status="pending"):
    """
    Show tasks in a given queue status.
    Usage: python3 task_manager.py list pending
    """
    if status not in ("pending", "approved", "in_progress", "completed", "rejected"):
        print("Invalid status. Use: pending | approved | in_progress | completed | rejected")
        return

    tasks = _load_tasks(status)
    if not tasks:
        print(f"No {status} tasks.")
        return

    print(f"\n=== {status.upper()} TASKS ({len(tasks)}) ===\n")
    for t in tasks:
        created = t.get("created_at", "N/A")
        if isinstance(created, str) and "T" in created:
            created = created.split("T")[1][:8]
        print(f"  ID:       {t.get('id','N/A')}")
        print(f"  Task:     {t.get('description','N/A')}")
        print(f"  Priority: {t.get('priority','normal')}")
        print(f"  Agent:    {t.get('assigned_agent','unassigned')}")
        print(f"  Created:  {created}")
        print(f"  Status:   {t.get('status', status)}")
        print()


def approve_task(task_id):
    """
    Move a task from pending to approved.
    Usage: python3 task_manager.py approve <task_id>
    """
    pending = QUEUE_DIR / "pending"
    approved = QUEUE_DIR / "approved"

    src = None
    for f in pending.glob("*.json"):
        with open(f) as fp:
            t = json.load(fp)
            if t.get("id") == task_id:
                src = f
                t["status"] = "approved"
                t["approved_at"] = datetime.now(timezone.utc).isoformat()
                break

    if not src:
        print(f"Task {task_id} not found in pending.")
        return

    dst = approved / src.name
    with open(dst, "w") as f:
        json.dump(t, f, indent=2)
    src.unlink()
    print(f"Approved: {task_id} -> approved/")


def reject_task(task_id):
    """
    Move a task from pending to rejected.
    Usage: python3 task_manager.py reject <task_id>
    """
    pending = QUEUE_DIR / "pending"
    rejected = QUEUE_DIR / "rejected"

    src = None
    for f in pending.glob("*.json"):
        with open(f) as fp:
            t = json.load(fp)
            if t.get("id") == task_id:
                src = f
                t["status"] = "rejected"
                t["rejected_at"] = datetime.now(timezone.utc).isoformat()
                break

    if not src:
        print(f"Task {task_id} not found in pending.")
        return

    dst = rejected / src.name
    with open(dst, "w") as f:
        json.dump(t, f, indent=2)
    src.unlink()
    print(f"Rejected: {task_id} -> rejected/")


def get_task(task_id):
    """
    Read full details of a task from any queue.
    Usage: python3 task_manager.py get <task_id>
    """
    for status in ("pending", "approved", "in_progress", "completed", "rejected"):
        dir_path = QUEUE_DIR / status
        for f in dir_path.glob("*.json"):
            with open(f) as fp:
                t = json.load(fp)
                if t.get("id") == task_id:
                    print(f"\n=== TASK: {task_id} ===")
                    print(json.dumps(t, indent=2))
                    return
    print(f"Task {task_id} not found in any queue.")


def submit_task(description, priority="normal", assigned_agent="", extra=None):
    """
    Programmatic task submission.
    Returns the task dict.
    """
    task = {
        "id": str(uuid.uuid4())[:8],
        "description": description,
        "priority": priority,
        "assigned_agent": assigned_agent,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        task.update(extra)

    path = QUEUE_DIR / "pending" / f"{task['id']}.json"
    with open(path, "w") as f:
        json.dump(task, f, indent=2)
    return task


def send_telegram_task(task):
    """Notify Master of a new task request."""
    import requests
    priority = task.get("priority", "normal")
    emoji = "🔴" if priority == "high" else "🟡" if priority == "medium" else "⚪"
    msg = (
        f"🔧 TASK REQUEST{emoji}\n\n"
        f"ID: {task['id']}\n"
        f"Priority: {priority.upper()}\n"
        f"Agent: {task.get('assigned_agent','unassigned')}\n\n"
        f"Task:\n{task['description']}\n\n"
        f"Approve: /task approve {task['id']}\n"
        f"Reject:  /task reject {task['id']}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Task Manager CLI")
        print("\nCommands:")
        print("  list <status>   — list tasks (pending|approved|in_progress|completed|rejected)")
        print("  approve <id>    — approve a pending task")
        print("  reject <id>    — reject a pending task")
        print("  get <id>        — show full task details")
        sys.exit(0)

    cmd, *args = sys.argv[1:]

    if cmd == "list" and args:
        list_tasks(args[0])
    elif cmd == "approve" and args:
        approve_task(args[0])
    elif cmd == "reject" and args:
        reject_task(args[0])
    elif cmd == "get" and args:
        get_task(args[0])
    else:
        print("Unknown command. Use: list | approve | reject | get")
