"""App-domain topics from Python over a real bus. Run under dbus-run-session
(pixi run test-py does)."""

import unittest

import agentbus

HUB = "dev.agentbus.test.PyTopics"


class TopicsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = agentbus.Hub(HUB)
        cls.publisher = agentbus.Topics(HUB)

    def test_retained_then_live_for_one_subject(self):
        self.publisher.publish("power.battery", "laptop", {"percent": 80.0, "charging": False})
        sub = agentbus.Topics(HUB).subscribe("power.*", "laptop")
        first = sub.next_event(timeout=5)
        self.assertEqual(first["topic"], "power.battery")
        self.assertEqual(first["payload"], {"percent": 80.0, "charging": False})

        self.assertEqual(self.publisher.publish("power.battery", "phone", {"percent": 10.0}), 0)
        self.assertEqual(self.publisher.publish("power.battery", "laptop", {"percent": 79.0}), 1)
        nxt = sub.next_event(timeout=5)
        self.assertEqual((nxt["subject"], nxt["payload"]["percent"]), ("laptop", 79.0))
        self.assertGreater(nxt["seq"], first["seq"])
        sub.unsubscribe()
        with self.assertRaises(agentbus.BusError):
            sub.next_event(timeout=1)

    def test_demand(self):
        watch = self.publisher.watch_demand()
        app = agentbus.Topics(HUB)
        sub = app.subscribe("presence.status", "did:plc:alice")
        want = {"pattern": "presence.status", "subject": "did:plc:alice"}
        self.assertEqual(watch.next_change(timeout=5), {**want, "subscribers": 1})
        sub.unsubscribe()
        self.assertEqual(watch.next_change(timeout=5), {**want, "subscribers": 0})
        with self.assertRaises(TimeoutError):
            watch.next_change(timeout=0.2)

    def test_patterns_and_bad_topics(self):
        self.assertTrue(agentbus.pattern_matches("weather.*", "weather.alert"))
        self.assertFalse(agentbus.pattern_matches("weather.current", "weather.alert"))
        with self.assertRaises(agentbus.BusError):
            self.publisher.publish("weather.*", "", {})


if __name__ == "__main__":
    unittest.main()
