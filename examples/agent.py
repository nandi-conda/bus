"""A planner agent that joins the org.agentbus.cap.Plan1 slot and serves until Ctrl-C.

Usage: python examples/agent.py [BUS_NAME]   (default org.agentbus.agent.planner)
"""

import sys
import time

import agentbus

SLOT = "org.agentbus.cap.Plan1"
CLASS = "dev.agentbus.task.plan"
SCHEMA = '{"type":"object","required":["language"],"properties":{"language":{"type":"string"}}}'


def plan(ctx):
    ctx.progress(10, "reading the goal")
    language = ctx.params.get("language") or ctx.ask(
        "Which language should the plan target?", SCHEMA
    )["language"]
    ctx.progress(60, f"planning in {language}")
    steps = [
        f"restate the goal: {ctx.goal}",
        f"sketch the {language} modules",
        "write tests first",
        "implement and iterate",
    ]
    ctx.progress(100, "done")
    return {"language": language, "steps": steps}


name = sys.argv[1] if len(sys.argv) > 1 else "org.agentbus.agent.planner"
agent = agentbus.Agent(name, [CLASS], plan)
# The router may still be starting; give it a few seconds before giving up.
for _ in range(50):
    try:
        agent.join_slot(SLOT)
        break
    except agentbus.BusError:
        time.sleep(0.1)
else:
    sys.exit(f"could not join {SLOT}")
print(f"agent: {name} joined {SLOT}", flush=True)
agent.run_forever()
