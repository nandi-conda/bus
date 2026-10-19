"""An app watching a topic: watch.py [PATTERN] [SUBJECT] [COUNT]
(default weather.* for Berlin, 5 events)."""

import sys

import agentbus

args = sys.argv[1:]
pattern = args[0] if len(args) > 0 else "weather.*"
subject = args[1] if len(args) > 1 else "Berlin"
count = int(args[2]) if len(args) > 2 else 5

sub = agentbus.Topics().subscribe(pattern, subject)
print(f"watch: subscribed to {pattern} for {subject!r}", flush=True)
for _, e in zip(range(count), sub):
    print(f"watch: #{e['seq']} {e['topic']} [{e['subject']}] {e['payload']}", flush=True)
sub.unsubscribe()
