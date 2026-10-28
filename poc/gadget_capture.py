#!/usr/bin/env python3
"""Persist every plaintext plan/limit match from the Frida gadget."""
import frida, hashlib, json, os, subprocess, sys, time

ADB = os.environ.get("ADB", "/Users/martinmarinov/Library/Android/sdk/platform-tools/adb")
HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "gadget_capture_fast.js")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "captures", "raw-commands.jsonl")

subprocess.run([ADB, "forward", "tcp:27042", "tcp:27042"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
seen = set()
out = open(OUT, "a", encoding="utf-8", buffering=1)
print(f"CAPTURING -> {OUT}", flush=True)

def on_message(message, data):
    if message.get("type") != "send":
        return
    payload = message.get("payload") or {}
    text = payload.get("data") or ""
    if not text or payload.get("tag") in ("ready", "error"):
        if payload.get("tag") == "ready":
            print("scanner ready", flush=True)
        return
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    if digest in seen:
        return
    seen.add(digest)
    rec = {"ts": int(time.time()), "tag": payload.get("tag"),
           "addr": payload.get("addr"), "data": text}
    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"HIT {payload.get('tag')} {len(text)} bytes", flush=True)

while True:
    try:
        device = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
        session = device.attach("Gadget")
        script = session.create_script(open(JS, encoding="utf-8").read())
        script.on("message", on_message)
        script.load()
        print("attached", flush=True)
        while True:
            time.sleep(5)
    except Exception as e:
        print(f"retry: {e}", flush=True)
        time.sleep(3)
