"""Own dev.agentbus.Hub: apps call functions and subscribe to topics through it."""

import agentbus

hub = agentbus.Hub()
print(f"hub: serving {agentbus.HUB_NAME}", flush=True)
hub.run_forever()
