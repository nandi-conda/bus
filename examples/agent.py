"""A Python planner agent that joins the org.agentbus.cap.Plan1 slot and serves until Ctrl-C."""

import sys
import time

import agentbus


def plan(ctx):
    ctx.progress(10, "python agent reading the goal")
    language = ctx.params.get("language") or ctx.ask(
        "Which language should the plan target?",
        '{"type":"object","required":["language"],"properties":{"language":{"type":"string"}}}',
    )["language"]
    ctx.log(f"planning {ctx.goal!r} in {language}")
    ctx.progress(100, "done")
    return {"language": language, "steps": [f"plan {ctx.goal} in {language}", "ship it"]}


agent = agentbus.Agent("org.agentbus.agent.pyplanner", ["dev.agentbus.task.plan"], plan)
for _ in range(50):
    try:
        agent.join_slot("org.agentbus.cap.Plan1")
        break
    except agentbus.BusError:
        time.sleep(0.1)
else:
    sys.exit("could not join org.agentbus.cap.Plan1")
print("python agent: joined org.agentbus.cap.Plan1")

agent.run_forever()
