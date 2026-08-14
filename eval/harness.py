import httpx
import time
import json
from pathlib import Path

BACKEND = "http://localhost:8000"


SCENARIOS = [
    {
        "name": "returning_patient_booking_en",
        "phone": "+919876543210",
        "language": "en",
        "script": [
            {"type": "lookup"},
            {"type": "search", "date_from": "2026-12-01", "date_to": "2026-12-31", "earliest_only": True},
            {"type": "book", "practitioner_id": 1, "branch_id": 1, "start_time": "2026-12-13T13:00:00", "end_time": "2026-12-13T13:30:00"},
        ],
    },
    {
        "name": "callback_resume_en",
        "phone": "+919876543210",
        "language": "en",
        "script": [
            {"type": "start_call"},
            {"type": "end_call"},
            {"type": "lookup"},
        ],
    },
    {
        "name": "earliest_slot_across_branches",
        "phone": "+910000000000",
        "language": "en",
        "script": [
            {"type": "search", "date_from": "2026-12-01", "date_to": "2026-12-02", "earliest_only": True},
        ],
    },
]


def measure_request(method: str, path: str, json_payload: dict | None = None):
    url = BACKEND + path
    start = time.time()
    if method.lower() == "post":
        r = httpx.post(url, json=json_payload)
    else:
        r = httpx.get(url)
    elapsed = time.time() - start
    return r, elapsed


def run_scenario(s):
    print(f"Running scenario: {s['name']}")
    metrics = {"steps": [], "language": s.get("language", "en"), "turns_to_completion": None}
    call_session_id = None
    for step in s["script"]:
        if step["type"] == "start_call":
            payload = {"livekit_room_name": "testroom", "phone_e164": s.get("phone"), "direction": "outbound"}
            r, t = measure_request("post", "/calls/start", payload)
            metrics["steps"].append({"action": "calls/start", "status": r.status_code, "time_s": t, "resp": r.json() if r.status_code == 200 else None})
            if r.status_code == 200:
                call_session_id = r.json().get("call_session_id")
        elif step["type"] == "end_call":
            payload = {"call_session_id": call_session_id, "outcome": "missed"}
            r, t = measure_request("post", "/calls/end", payload)
            metrics["steps"].append({"action": "calls/end", "status": r.status_code, "time_s": t})
        elif step["type"] == "lookup":
            payload = {"phone_e164": s.get("phone")}
            r, t = measure_request("post", "/tools/lookup_patient", payload)
            metrics["steps"].append({"action": "lookup_patient", "status": r.status_code, "time_s": t, "resp": r.json() if r.status_code == 200 else None})
        elif step["type"] == "search":
            payload = {"date_from": step["date_from"], "date_to": step["date_to"], "earliest_only": step.get("earliest_only", False)}
            r, t = measure_request("post", "/tools/search_availability", payload)
            metrics["steps"].append({"action": "search_availability", "status": r.status_code, "time_s": t, "resp_count": len(r.json().get("slots", [])) if r.status_code == 200 else None})
        elif step["type"] == "book":
            payload = {
                "idempotency_key": f"test:{s['name']}",
                "patient_id": None,
                "practitioner_id": step["practitioner_id"],
                "branch_id": step["branch_id"],
                "start_time": step["start_time"],
                "end_time": step["end_time"],
                "call_session_id": call_session_id,
            }
            r, t = measure_request("post", "/tools/book_appointment", payload)
            metrics["steps"].append({"action": "book_appointment", "status": r.status_code, "time_s": t, "resp": r.json() if r.status_code in (200,201) else r.text})
            if r.status_code == 200:
                metrics["turns_to_completion"] = len(metrics["steps"])
    return metrics


def main():
    results = {}
    for s in SCENARIOS:
        results[s["name"]] = run_scenario(s)
    out = Path("eval_results.json")
    out.write_text(json.dumps(results, indent=2))
    print("Wrote eval_results.json")


if __name__ == "__main__":
    main()
