"""Submit a planning task to the org.agentbus.cap.Plan1 slot and answer its question."""

import sys

import agentbus

SLOT = "org.agentbus.cap.Plan1"

task = agentbus.Client().submit(
    SLOT, "dev.agentbus.task.plan", "build a tiny key-value store", params={}
)
print(f"python: submitted {task.path} to {SLOT}")
for event in task:
    if event["type"] == "question":
        print(f"python: question {event['id']}: {event['text']} -> python")
        task.respond(event["id"], {"language": "python"})
    elif event["type"] == "progress":
        print(f"python: progress {event['pct']:5.1f}% {event['note']}")
    else:
        print(f"python: {event}")

result = task.result()
print(f"python: {result['state']}: {result['deliverable']}")
sys.exit(0 if result["state"] == "completed" else 1)
