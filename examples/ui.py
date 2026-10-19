"""A terminal proc that provides ui.ask and ui.confirm, so any proc can ask the person at this
terminal a question by calling a function."""

import json

import agentbus

procs = agentbus.Procs()


@procs.function("ui.ask", schema=json.dumps({"result": {"type": "object", "required": ["answer"]}}))
def ask(text, schema=""):
    hint = f" (schema {schema})" if schema else ""
    return {"answer": input(f"? {text}{hint} ")}


@procs.function("ui.confirm", schema=json.dumps({"result": {"type": "object", "required": ["ok"]}}))
def confirm(text):
    return {"ok": input(f"? {text} [y/N] ").strip().lower() in ("y", "yes")}


print("ui: providing ui.ask and ui.confirm", flush=True)
procs.run_forever()
