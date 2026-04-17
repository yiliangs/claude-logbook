"""Activity-log retrieval primitives for the /audit slash command.

Two subcommands, both operate over short_log/*.jsonl:

    filter     — mechanical filter over structured fields + keywords
    aggregate  — group-by + metric aggregation

Reads of long_logs and project_cards are NOT handled here — Claude's Read
tool is sufficient for those. This file exists to move cheap bulk operations
(filtering thousands of entries, computing distributions) out of the LLM.

The agent chains these with its own semantic expansion + synthesis.

Outputs JSON (jsonl for filter, json dict for aggregate) by default so the
agent can parse cleanly. `--format table` prints human-readable tables.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHORT_LOG_DIR = os.path.join(SCRIPT_DIR, "short_log")


# ---------- time parsing ----------

_RELATIVE_RE = re.compile(r"^\s*(\d+)\s+(hour|day|week|month)s?\s+ago\s*$", re.IGNORECASE)

def parse_when(s):
    """Accept: ISO 8601, YYYY-MM-DD, 'N {hour|day|week|month}s ago', 'today', 'yesterday'.
    Returns tz-aware datetime (local tz). None if s is None/empty."""
    if not s:
        return None
    s = s.strip()
    now = datetime.now().astimezone()
    lower = s.lower()
    if lower == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if lower == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    m = _RELATIVE_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }[unit]
        return now - delta
    # ISO 8601 or YYYY-MM-DD
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return dt
    except ValueError:
        raise SystemExit(f"audit: cannot parse time '{s}'")


def entry_time(e):
    ts = e.get("timestamp") or ""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------- short_log loader ----------

def iter_short_log_shards(since=None, until=None):
    """Yield shard paths (YYYY-MM.jsonl) that overlap [since, until].
    Cheap prune: skip shards whose month is strictly outside the range.
    """
    if not os.path.isdir(SHORT_LOG_DIR):
        return
    shards = sorted(f for f in os.listdir(SHORT_LOG_DIR) if f.endswith(".jsonl"))
    for name in shards:
        stem = name[:-6]  # YYYY-MM
        try:
            year, mon = stem.split("-")
            shard_start = datetime(int(year), int(mon), 1, tzinfo=since.tzinfo if since else timezone.utc)
            if mon == "12":
                shard_end = datetime(int(year) + 1, 1, 1, tzinfo=shard_start.tzinfo)
            else:
                shard_end = datetime(int(year), int(mon) + 1, 1, tzinfo=shard_start.tzinfo)
        except (ValueError, IndexError):
            yield os.path.join(SHORT_LOG_DIR, name)
            continue
        if since and shard_end <= since:
            continue
        if until and shard_start > until:
            continue
        yield os.path.join(SHORT_LOG_DIR, name)


def load_entries(since=None, until=None):
    for path in iter_short_log_shards(since, until):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


# ---------- filter ----------

def entry_matches(e, *, since, until, project, branch, status, session_id,
                  keywords, keyword_mode, has_ai):
    ts = entry_time(e)
    if since and (ts is None or ts < since):
        return False
    if until and (ts is None or ts > until):
        return False
    if project and e.get("project") != project:
        return False
    if branch and e.get("git_branch") != branch:
        return False
    if status and e.get("status") != status:
        return False
    if session_id and e.get("session_id") != session_id:
        return False
    if has_ai is True and not (e.get("question_summary") or e.get("response_core")):
        return False
    if has_ai is False and (e.get("question_summary") or e.get("response_core")):
        return False
    if keywords:
        hay_parts = [e.get("question_summary") or "", e.get("response_core") or ""]
        hay_parts.extend(e.get("artifacts") or [])
        hay = "\n".join(hay_parts).lower()
        hits = [kw.lower() in hay for kw in keywords]
        if keyword_mode == "all" and not all(hits):
            return False
        if keyword_mode == "any" and not any(hits):
            return False
    return True


def cmd_filter(args):
    since = parse_when(args.since)
    until = parse_when(args.until)
    has_ai = {"yes": True, "no": False, "any": None}[args.has_ai]
    keywords = [k for k in (args.keyword or []) if k]

    out = []
    for e in load_entries(since, until):
        if entry_matches(e, since=since, until=until, project=args.project,
                         branch=args.branch, status=args.status,
                         session_id=args.session_id, keywords=keywords,
                         keyword_mode=args.keyword_mode, has_ai=has_ai):
            out.append(e)
            if args.limit and len(out) >= args.limit:
                break

    if args.format == "session-ids":
        seen = []
        for e in out:
            sid = e.get("session_id")
            if sid and sid not in seen:
                seen.append(sid)
        for sid in seen:
            print(sid)
        return 0

    if args.format == "table":
        print_entries_table(out)
        return 0

    for e in out:
        print(json.dumps(e, ensure_ascii=False))
    return 0


def print_entries_table(entries):
    if not entries:
        print("(no entries)")
        return
    for e in entries:
        ts = (e.get("timestamp") or "")[:16].replace("T", " ")
        sid = (e.get("session_id") or "")[:8]
        turn = e.get("turn", "?")
        proj = (e.get("project") or "")[:18]
        status = (e.get("status") or "")[:10]
        q = (e.get("question_summary") or "").replace("\n", " ")[:80]
        print(f"{ts}  {sid}#{turn:<3}  {proj:<18}  {status:<10}  {q}")


# ---------- aggregate ----------

_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def group_key(entry, group_by):
    ts = entry_time(entry)
    if group_by == "weekday":
        return _WEEKDAY[ts.weekday()] if ts else None
    if group_by == "hour":
        return ts.hour if ts else None
    if group_by == "day":
        return ts.date().isoformat() if ts else None
    if group_by == "project":
        return entry.get("project") or "(none)"
    if group_by == "branch":
        return entry.get("git_branch") or "(none)"
    if group_by == "status":
        return entry.get("status") or "(none)"
    raise SystemExit(f"audit: unknown group_by '{group_by}'")


def session_durations(entries):
    """Return {session_id: minutes} from first-to-last timestamp per session."""
    by_sid = defaultdict(list)
    for e in entries:
        sid = e.get("session_id")
        ts = entry_time(e)
        if sid and ts:
            by_sid[sid].append(ts)
    return {
        sid: max(0.0, (max(t) - min(t)).total_seconds() / 60.0)
        for sid, t in by_sid.items()
    }


def cmd_aggregate(args):
    since = parse_when(args.since)
    until = parse_when(args.until)
    has_ai = {"yes": True, "no": False, "any": None}[args.has_ai]
    keywords = [k for k in (args.keyword or []) if k]

    filtered = []
    for e in load_entries(since, until):
        if entry_matches(e, since=since, until=until, project=args.project,
                         branch=args.branch, status=args.status,
                         session_id=None, keywords=keywords,
                         keyword_mode=args.keyword_mode, has_ai=has_ai):
            filtered.append(e)

    durations = session_durations(filtered) if args.metric in (
        "session_duration_minutes_mean", "session_duration_minutes_sum") else {}

    buckets = defaultdict(list)
    for e in filtered:
        key = group_key(e, args.group_by)
        buckets[key].append(e)

    result = {}
    for key, items in buckets.items():
        if args.metric == "turns":
            v = len(items)
        elif args.metric == "sessions":
            v = len({e.get("session_id") for e in items if e.get("session_id")})
        elif args.metric == "session_duration_minutes_mean":
            sids = {e.get("session_id") for e in items if e.get("session_id")}
            vals = [durations[s] for s in sids if s in durations]
            v = round(sum(vals) / len(vals), 1) if vals else 0.0
        elif args.metric == "session_duration_minutes_sum":
            sids = {e.get("session_id") for e in items if e.get("session_id")}
            v = round(sum(durations.get(s, 0.0) for s in sids), 1)
        elif args.metric == "status_ratio":
            total = len(items)
            resolved = sum(1 for e in items if e.get("status") == "resolved")
            v = {
                "resolved": resolved,
                "unresolved": sum(1 for e in items if e.get("status") == "unresolved"),
                "blocked": sum(1 for e in items if e.get("status") == "blocked"),
                "total": total,
                "resolved_pct": round(100.0 * resolved / total, 1) if total else 0.0,
            }
        else:
            raise SystemExit(f"audit: unknown metric '{args.metric}'")
        result[str(key)] = v

    # Sort keys sensibly
    if args.group_by == "weekday":
        ordered = sorted(result.items(), key=lambda kv: _WEEKDAY.index(kv[0]) if kv[0] in _WEEKDAY else 99)
    elif args.group_by == "hour":
        ordered = sorted(result.items(), key=lambda kv: int(kv[0]))
    else:
        ordered = sorted(result.items())

    if args.format == "table":
        print_aggregate_table(ordered, args.group_by, args.metric)
        return 0

    print(json.dumps({
        "group_by": args.group_by,
        "metric": args.metric,
        "filters": {
            "since": args.since, "until": args.until,
            "project": args.project, "branch": args.branch,
            "status": args.status, "keywords": keywords,
            "has_ai": args.has_ai,
        },
        "result": dict(ordered),
        "total_entries": len(filtered),
    }, ensure_ascii=False, indent=2))
    return 0


def print_aggregate_table(ordered, group_by, metric):
    if not ordered:
        print("(no data)")
        return
    header = f"{group_by:<12}  {metric}"
    print(header)
    print("-" * len(header))
    for k, v in ordered:
        if isinstance(v, dict):
            print(f"{k:<12}  {v}")
        else:
            print(f"{k:<12}  {v}")


# ---------- argparse ----------

def build_parser():
    p = argparse.ArgumentParser(
        prog="audit",
        description="Retrieval primitives over short_log for the /audit command."
    )
    sp = p.add_subparsers(dest="cmd", required=True)

    def add_shared(sub, include_session=True):
        sub.add_argument("--since", help="ISO date, 'N days ago', 'yesterday', 'today'")
        sub.add_argument("--until", help="ISO date or relative; exclusive upper bound")
        sub.add_argument("--project", help="project slug (exact match)")
        sub.add_argument("--branch", help="git_branch (exact match)")
        sub.add_argument("--status", choices=["resolved", "unresolved", "blocked"])
        if include_session:
            sub.add_argument("--session-id", dest="session_id", help="exact session_id")
        sub.add_argument("--keyword", action="append",
                         help="substring match against question_summary + response_core + artifacts "
                              "(case-insensitive; repeat flag for multiple)")
        sub.add_argument("--keyword-mode", choices=["any", "all"], default="any",
                         help="combine multiple --keyword args with OR (any) or AND (all); default any")
        sub.add_argument("--has-ai", choices=["yes", "no", "any"], default="any",
                         help="require/exclude entries with AI-summary fields filled")

    fp = sp.add_parser("filter", help="Return matching short_log entries")
    add_shared(fp, include_session=True)
    fp.add_argument("--limit", type=int, default=0, help="cap output (0 = no cap)")
    fp.add_argument("--format", choices=["jsonl", "table", "session-ids"], default="jsonl")
    fp.set_defaults(func=cmd_filter)

    ap = sp.add_parser("aggregate", help="Group-by + metric over short_log")
    add_shared(ap, include_session=False)
    ap.add_argument("--group-by", required=True,
                    choices=["weekday", "hour", "day", "project", "branch", "status"])
    ap.add_argument("--metric", required=True,
                    choices=["turns", "sessions", "session_duration_minutes_mean",
                             "session_duration_minutes_sum", "status_ratio"])
    ap.add_argument("--format", choices=["json", "table"], default="json")
    ap.set_defaults(func=cmd_aggregate)

    return p


def main():
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
