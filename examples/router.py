"""Own a capability slot (default org.agentbus.cap.Plan1) and route its tasks to its agents."""

import sys

import agentbus

slot = sys.argv[1] if len(sys.argv) > 1 else "org.agentbus.cap.Plan1"
router = agentbus.Router(slot)
print(f"router: serving {slot}", flush=True)
router.run_forever()
