"""
test_bot.py
Runs the "practice exam": sends every message in data/test_messages.csv to the
n8n workflow, compares the bot's answer with the correct answer, and prints a score.

It checks two things for each message:
  1. Intent  - did the AI understand what the customer wants?
  2. Route   - did the message go to the right place (send now / draft / human / urgent)?

The most important number is "unsafe auto-sends": messages that SHOULD have gone to
a human but were answered automatically. That must be 0.

Uses only built-in Python (no pip install needed).

Run:
  python scripts/test_bot.py --url https://YOUR-N8N/webhook/ws-support
  python scripts/test_bot.py --url ... --limit 10        (quick test on 10 messages)
  python scripts/test_bot.py --url ... --start 60        (continue from message 60)
  python scripts/test_bot.py --url ... --rerun-busy      (retry only messages the AI missed)
  python scripts/test_bot.py --summary                   (final score for all saved results)

Results for every message are kept in results/test_results.csv (one row per message);
running a message again replaces its old row.
"""

import argparse
import csv
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "data" / "test_messages.csv"
RESULTS = ROOT / "results" / "test_results.csv"

LEVEL = {"AUTO_SEND": 0, "DRAFT": 1, "HUMAN": 2, "URGENT": 3}

# Rough cost estimate. ASSUMPTION: check current Gemini pricing before quoting.
# About 4 characters = 1 token. Each call sends the full knowledge base + instructions.
PROMPT_CHARS = 12000          # instructions + kb.md + message, measured roughly
OUTPUT_CHARS = 800            # JSON answer with reply
USD_PER_M_INPUT = 0.10        # Gemini Flash-Lite, per million input tokens (check)
USD_PER_M_OUTPUT = 0.40       # per million output tokens (check)
USD_TO_INR = 84


def send(url, row, timeout=90):
    """Send one message to n8n and return its JSON answer."""
    payload = json.dumps({
        "msg_id": row["msg_id"],
        "channel": row["channel"],
        "subject": row["subject"],
        "text": row["text"],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


FIELDS = ["msg_id", "text", "expected_intent", "got_intent", "intent_ok",
          "expected_route", "got_route", "route_ok", "unsafe", "seconds", "why", "reply", "error"]


def load_saved():
    """Read earlier results (one row per message). Later runs replace older rows."""
    if not RESULTS.exists():
        return {}
    saved = {}
    for r in csv.DictReader(open(RESULTS, encoding="utf-8")):
        for k in ("intent_ok", "route_ok", "unsafe"):
            r[k] = r[k] == "True"
        r["seconds"] = float(r["seconds"] or 0)
        saved[r["msg_id"]] = r
    return saved


def save_all(saved):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for msg_id in sorted(saved):
            writer.writerow({k: saved[msg_id].get(k, "") for k in FIELDS})


def needs_rerun(r):
    """No real AI answer yet: network error, or AI was busy and it went to a person."""
    return bool(r["error"]) or r["why"].startswith("AI unavailable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="n8n webhook Production URL")
    ap.add_argument("--limit", type=int, default=0, help="only run this many messages")
    ap.add_argument("--start", type=int, default=1, help="start from this message number")
    ap.add_argument("--gap", type=float, default=12.0, help="seconds to wait between messages (free AI limit)")
    ap.add_argument("--rerun-busy", action="store_true",
                    help="only re-send messages that got no AI answer last time")
    ap.add_argument("--summary", action="store_true", help="only print the score for all saved results")
    args = ap.parse_args()

    saved = load_saved()
    if args.summary:
        print_overall(saved)
        return
    if not args.url:
        ap.error("--url is needed to send messages")

    rows = list(csv.DictReader(open(MESSAGES, encoding="utf-8")))
    if args.rerun_busy:
        rows = [r for r in rows if r["msg_id"] in saved and needs_rerun(saved[r["msg_id"]])]
        print(f"Re-running {len(rows)} messages that had no AI answer.")
    else:
        rows = [r for r in rows if int(r["msg_id"][1:]) >= args.start]
    if args.limit:
        rows = rows[: args.limit]

    results = []
    for i, row in enumerate(rows, start=1):
        start = time.time()
        rec = {"msg_id": row["msg_id"], "text": row["text"],
               "expected_intent": row["expected_intent"], "expected_route": row["expected_route"],
               "got_intent": "", "got_route": "", "why": "", "reply": "", "error": ""}
        try:
            ans = send(args.url, row)
            rec["got_intent"] = ans.get("intent", "")
            rec["got_route"] = ans.get("route", "")
            rec["why"] = ans.get("why", "")
            rec["reply"] = ans.get("reply", "")
        except Exception as e:  # network error, n8n error, AI limit hit...
            rec["error"] = str(e)[:200]
        rec["seconds"] = round(time.time() - start, 1)
        rec["intent_ok"] = rec["got_intent"] == row["expected_intent"]
        rec["route_ok"] = rec["got_route"] == row["expected_route"]
        # Unsafe = should have gone to a person, but was sent automatically
        rec["unsafe"] = (rec["got_route"] == "AUTO_SEND" and LEVEL[row["expected_route"]] >= LEVEL["HUMAN"])
        saved[rec["msg_id"]] = rec
        save_all(saved)  # save after every message, so nothing is lost if it stops
        results.append(rec)

        mark = "OK " if rec["route_ok"] else ("ERR" if rec["error"] else "X  ")
        print(f"[{i}/{len(rows)}] {mark} {row['msg_id']} expected {row['expected_route']:<9} "
              f"got {rec['got_route'] or '-':<9} {rec['seconds']}s  {row['text'][:50]}")
        time.sleep(args.gap)

    print("\n---------- THIS RUN ----------")
    summary(results)
    print_overall(saved)


def print_overall(saved):
    total = len(saved)
    if not total:
        print("No saved results yet.")
        return
    print(f"\n========== ALL SAVED RESULTS ({total} of 120 messages) ==========")
    still_busy = [r for r in saved.values() if needs_rerun(r)]
    if still_busy:
        print(f"Still without an AI answer: {len(still_busy)}  -> run again with --rerun-busy")
    summary(list(saved.values()))


def summary(results):
    answered = [r for r in results if not r["error"]]
    # Messages where Gemini was busy/down: safely sent to a person, but not an AI answer
    ai_failed = [r for r in answered if r["why"].startswith("AI unavailable")]
    done = [r for r in answered if r not in ai_failed]
    n = len(done)
    if n == 0:
        print("\nNo AI answers. Check the URL, that the workflow is Published, and the AI limit.")
        return
    intent_acc = sum(r["intent_ok"] for r in done) / n
    route_acc = sum(r["route_ok"] for r in done) / n
    unsafe = [r for r in done if r["unsafe"]]
    auto = sum(1 for r in done if r["got_route"] == "AUTO_SEND")
    avg_s = sum(r["seconds"] for r in done) / n

    cost_usd = (PROMPT_CHARS / 4 * USD_PER_M_INPUT + OUTPUT_CHARS / 4 * USD_PER_M_OUTPUT) / 1_000_000
    per_day_inr = cost_usd * 2000 * USD_TO_INR

    print("\n========== RESULTS ==========")
    print(f"Messages answered by AI : {n} of {len(results)}")
    print(f"AI busy -> sent to human: {len(ai_failed)}   (safe fallback, not counted below)")
    print(f"Script/network errors   : {len(results) - len(answered)}")
    print(f"Intent correct          : {intent_acc:.0%}")
    print(f"Route correct           : {route_acc:.0%}")
    print(f"Answered automatically  : {auto} ({auto / n:.0%})")
    print(f"UNSAFE auto-sends       : {len(unsafe)}   <- must be 0")
    print(f"Average time per message: {avg_s:.1f} s")
    print(f"Estimated AI cost       : about Rs {cost_usd * USD_TO_INR:.3f} per message, "
          f"Rs {per_day_inr:.0f} per day at 2,000 messages")
    for r in unsafe:
        print(f"  UNSAFE {r['msg_id']}: {r['text'][:70]}")
    print(f"Full results saved to {RESULTS}")


if __name__ == "__main__":
    main()
