"""The zbus greeter, as an agent: answers "dev.agentbus.task.greet" tasks at org.agentbus.Greeter.

Try it with: python examples/greeter.py --call Maria
"""

import sys

import agentbus

NAME = "org.agentbus.Greeter"
CLASS = "dev.agentbus.task.greet"

count = 0


def greet(ctx):
    global count
    count += 1
    return {"greeting": f"Hello {ctx.params['name']}! I have been called {count} times."}


if len(sys.argv) == 3 and sys.argv[1] == "--call":
    task = agentbus.Client().submit(NAME, CLASS, "say hello", params={"name": sys.argv[2]})
    for _ in task:
        pass
    print(task.result()["deliverable"]["greeting"])
else:
    agent = agentbus.Agent(NAME, [CLASS], greet)
    print(f"greeter: serving {NAME}")
    agent.run_forever()
