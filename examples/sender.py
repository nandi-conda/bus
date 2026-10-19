"""Submit a planning task to the org.agentbus.cap.Plan1 slot, answer its question, print the plan."""

import sys

import agentbus

SLOT = "org.agentbus.cap.Plan1"
language = sys.argv[1] if len(sys.argv) > 1 else "python"

task = agentbus.Client().submit(SLOT, "dev.agentbus.task.plan", "build a tiny key-value store")
print(f"sender: submitted {task.path}")
for event in task:
    kind = event["type"]
    if kind == "progress":
        print(f"sender: progress {event['pct']:5.1f}% {event['note']}")
    elif kind == "log":
        print(f"sender: log [{event['level']}] {event['msg']}")
    elif kind == "question":
        print(f"sender: question {event['id']}: {event['text']} -> {language}")
        task.respond(event["id"], {"language": language})
    else:
        print(f"sender: finished {event['state']}")

result = task.result()
print(f"sender: deliverable {result['deliverable']}")
sys.exit(0 if result["state"] == "completed" else 1)
