"""Own dev.agentbus.Topics, the hub apps subscribe to and services publish through."""

import agentbus

hub = agentbus.TopicsHub()
print(f"hub: serving {agentbus.TOPICS_NAME}", flush=True)
hub.run_forever()
