"""Long-log synthesizer.

For each unsynthesized session (transcripts/<id>.md exists but long_logs/<id>.yaml does not):

1. Call LLM #1 (long-log synthesis) — inputs: raw transcript + session metadata
2. Call LLM #2 (project-card update) — inputs: just-written long log + existing card
   + up to 3 prior long logs (so Kimi can judge pivots across sessions, not from
   a single tangent).
3. Sanitize card (mechanical post-pass): scrub maintenance artifacts, drop stale
   threads/artifacts, flag orphan ids, check token + thread-count caps.
4. Commit + push the results

A separate prune pass deletes raw transcripts older than `--prune-older-than DAYS`
when a corresponding long log exists — the retention window lets you re-synthesize
if a long log turns out wrong.

Designed for a scheduled routine (Claude Schedule remote agent, cron, Task Scheduler,
or manual invocation). Idempotent and crash-resilient: if anything fails, the
transcript stays and retries on the next run.

Usage:
    python synthesizer.py                          # process all unsynthesized + prune 30d default
    python synthesizer.py --limit 5                # cap synthesis per run (oldest first)
    python synthesizer.py --prune-older-than 60    # override retention window
    python synthesizer.py --no-prune               # skip prune pass
    python synthesizer.py --dry-run                # don't write, delete, or push
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from _api import call_llm, configured  # noqa: E402

LOG_REPO = SCRIPT_DIR
SHORT_LOG_DIR = os.path.join(LOG_REPO, "short_log")
TRANSCRIPTS_DIR = os.path.join(LOG_REPO, "transcripts")
LONG_LOGS_DIR = os.path.join(LOG_REPO, "long_logs")
PROJECT_CARDS_DIR = os.path.join(LOG_REPO, "project_cards")
SCHEMAS_DIR = os.path.join(LOG_REPO, "schemas")

MAX_TRANSCRIPT_CHARS = 100_000
DEFAULT_PRUNE_DAYS = 30
DEFAULT_IDLE_MINUTES = 60       # skip transcripts touched within this window
                                # (session might still be active — would miss later turns)
PIVOT_CONTEXT_SESSIONS = 3      # prior long logs to pass alongside current for pivot detection
CARD_TOKEN_CAP = 1500           # per schema; sanitize flags if exceeded
THREAD_SPLIT_THRESHOLD = 8      # > N active threads → flag card for split
STALE_SESSION_AGE = 5           # threads/artifacts older than N sessions get dropped

MAINTENANCE_ARTIFACT_PATTERNS = (
    ".gitignore", ".gitattributes", "CODEOWNERS", ".editorconfig",
    "package-lock.json", "poetry.lock", "Pipfile.lock", "yarn.lock",
    "uv.lock", "Cargo.lock", "Gemfile.lock",
)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ---------- git helpers ----------

def git(args, check=True):
    """Run a git command in LOG_REPO. Returns (ok, stdout)."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=LOG_REPO, capture_output=True, text=True, timeout=30,
        )
        if check and r.returncode != 0:
            return False, r.stderr.strip()
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def git_pull():
    ok, out = git(["pull", "--ff-only", "--quiet"], check=False)
    if not ok:
        print(f"  warn: git pull failed: {out}")


def git_commit_push(message, paths):
    """Stage given paths, commit with message, push. No-op if nothing to commit."""
    if not paths:
        return
    git(["add", "--"] + paths, check=False)
    # Check if there's anything staged
    ok, _ = git(["diff", "--cached", "--quiet"], check=False)
    if ok:  # exit 0 from `diff --cached --quiet` means nothing staged
        return
    git(["commit", "-m", message, "--quiet"], check=False)
    ok, out = git(["push", "--quiet"], check=False)
    if not ok:
        print(f"  warn: git push failed: {out}")


# ---------- scanning ----------

def find_unsynthesized(idle_minutes=DEFAULT_IDLE_MINUTES):
    """Return list of (session_id, transcript_path, mtime), oldest first.

    Skips transcripts touched within `idle_minutes` — those sessions may
    still be active, and processing them now would produce a long log that
    blocks future turns from being synthesized.
    """
    if not os.path.isdir(TRANSCRIPTS_DIR):
        return []
    cutoff = time.time() - idle_minutes * 60
    out = []
    for fname in os.listdir(TRANSCRIPTS_DIR):
        if not fname.endswith(".md") or fname == ".gitkeep":
            continue
        session_id = fname[:-3]
        transcript_path = os.path.join(TRANSCRIPTS_DIR, fname)
        long_log_path = os.path.join(LONG_LOGS_DIR, f"{session_id}.yaml")
        if os.path.isfile(long_log_path):
            continue
        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError:
            continue
        if mtime > cutoff:
            print(f"[skip] {session_id} — touched within {idle_minutes}m, may still be active")
            continue
        out.append((session_id, transcript_path, mtime))
    out.sort(key=lambda x: x[2])
    return out


def gather_session_metadata(session_id):
    """Pull mechanical metadata for this session from short_log shards."""
    if not os.path.isdir(SHORT_LOG_DIR):
        return {}
    entries = []
    for fname in sorted(os.listdir(SHORT_LOG_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(SHORT_LOG_DIR, fname), encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("session_id") == session_id:
                        entries.append(e)
        except Exception:
            continue
    if not entries:
        return {"session_id": session_id}
    entries.sort(key=lambda e: e.get("turn", 0))
    first, last = entries[0], entries[-1]
    duration = None
    try:
        s = datetime.fromisoformat(first.get("timestamp", ""))
        e = datetime.fromisoformat(last.get("timestamp", ""))
        duration = max(0, int((e - s).total_seconds() / 60))
    except Exception:
        pass
    return {
        "session_id": session_id,
        "started_at": first.get("timestamp", ""),
        "ended_at": last.get("timestamp", ""),
        "duration_minutes": duration,
        "machine": first.get("machine", ""),
        "project": first.get("project", ""),
        "git_branch": first.get("git_branch", ""),
        "transcript_turns": len(entries),
    }


# ---------- schema loading ----------

_schema_cache = {}

def load_schema(name):
    if name in _schema_cache:
        return _schema_cache[name]
    path = os.path.join(SCHEMAS_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        text = ""
    _schema_cache[name] = text
    return text


# ---------- prompt assembly ----------

LONG_LOG_SYSTEM = (
    "You synthesize raw developer conversation transcripts into structured "
    "long logs. Follow the output rules, tone rules, and schema in the user "
    "message. Return valid YAML only, no prose outside the YAML structure."
)

PROJECT_CARD_SYSTEM = (
    "You update a per-project state snapshot (project_card) based on a newly "
    "synthesized session long log plus up to 3 prior long logs for pivot "
    "context. The card is a superset merge — preserve accumulated state; only "
    "replace when explicitly superseded. Replace `current_focus` only if pivot "
    "is visible across the prior sessions AND the current one, not from a "
    "single tangent. Follow the output rules, tone rules, and schema in the "
    "user message. Return valid YAML only, no prose outside the YAML structure."
)


def build_long_log_user_msg(session_meta, transcript_text):
    output_rules = load_schema("output_rules.md")
    tone = load_schema("tone.md")
    schema = load_schema("long_log_schema.md")

    meta_yaml = "\n".join(f"  {k}: {v}" for k, v in session_meta.items())

    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        transcript_text = (
            "[... earlier turns truncated to fit context window ...]\n\n"
            + transcript_text[-MAX_TRANSCRIPT_CHARS:]
        )

    return f"""# OUTPUT RULES

{output_rules}

# TONE

{tone}

# LONG LOG SCHEMA

{schema}

# SESSION METADATA (hook-provided ground truth — copy into `session:` block verbatim)

```yaml
session:
{meta_yaml}
```

# RAW TRANSCRIPT

{transcript_text}

# TASK

Produce the long log YAML now. Follow the schema exactly. Start directly with the YAML — no markdown fencing, no preamble.
"""


def build_project_card_user_msg(project, existing_card, new_long_log, prior_long_logs):
    output_rules = load_schema("output_rules.md")
    tone = load_schema("tone.md")
    schema = load_schema("project_card_schema.md")

    if existing_card is None:
        existing_block = (
            "No existing card for this project. This is the first session "
            "contributing to it. Create a new card.\n"
        )
    else:
        existing_block = f"```yaml\n{existing_card}\n```\n"

    if prior_long_logs:
        prior_parts = [
            f"## Prior session {i + 1} (older → newer)\n\n```yaml\n{log}\n```"
            for i, log in enumerate(prior_long_logs)
        ]
        prior_block = (
            "The following are the most recent prior long logs for this project. "
            "Use them to judge whether `current_focus` has genuinely pivoted or "
            "the current session is a tangent.\n\n" + "\n\n".join(prior_parts)
        )
    else:
        prior_block = "No prior long logs for this project — this session is the first contributor."

    return f"""# OUTPUT RULES

{output_rules}

# TONE

{tone}

# PROJECT CARD SCHEMA

{schema}

# EXISTING PROJECT CARD (project: {project})

{existing_block}

# PRIOR SESSIONS (pivot context)

{prior_block}

# NEW SESSION LONG LOG

```yaml
{new_long_log}
```

# TASK

Produce the updated project_card YAML. Merge new session findings into the existing card as a superset — preserve accumulated state, only replace when this session explicitly supersedes. Replace `current_focus` only if pivot is visible across prior sessions AND the new one. Start directly with the YAML — no markdown fencing, no preamble.
"""


# ---------- project card I/O ----------

def load_project_card(project):
    if not project:
        return None
    path = os.path.join(PROJECT_CARDS_DIR, f"{project}.yaml")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def load_prior_long_logs(project, exclude_session_id, limit=PIVOT_CONTEXT_SESSIONS):
    """Load the N most recent prior long logs for this project (chronological, oldest first).

    Excludes the current session's long log if present. Returns raw YAML strings.
    """
    if not project or not os.path.isdir(LONG_LOGS_DIR):
        return []
    candidates = []
    for fname in os.listdir(LONG_LOGS_DIR):
        if not fname.endswith(".yaml"):
            continue
        sid = fname[:-5]
        if sid == exclude_session_id:
            continue
        path = os.path.join(LONG_LOGS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        # Cheap project filter — match `project: <slug>` on a session line
        if f"project: {project}" not in text:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, text))
    candidates.sort(key=lambda x: x[0])
    return [text for _, text in candidates[-limit:]]


def write_yaml(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")


# ---------- response cleanup ----------

def strip_fences(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```$", "", raw)
    return raw.strip()


# ---------- per-session synthesis ----------

def synthesize_one(session_id, transcript_path, dry_run=False):
    print(f"[synth] {session_id}")

    try:
        with open(transcript_path, encoding="utf-8") as f:
            transcript_text = f.read()
    except Exception as e:
        print(f"  ERROR reading transcript: {e}")
        return False

    session_meta = gather_session_metadata(session_id)
    project = session_meta.get("project", "")

    # --- Call #1: long log synthesis ---
    user_msg = build_long_log_user_msg(session_meta, transcript_text)

    if dry_run:
        print(f"  [dry-run] project={project} long-log input_chars={len(user_msg)}")
        return True

    started = time.time()
    raw = call_llm(LONG_LOG_SYSTEM, user_msg, max_tokens=4096, temperature=0.2,
                   synth=True, timeout=120)
    long_elapsed = int(time.time() - started)
    if not raw:
        print(f"  long-log API call failed (elapsed={long_elapsed}s)")
        return False

    long_log_yaml = strip_fences(raw)
    long_log_yaml = inject_synthesis_meta(long_log_yaml, long_elapsed, session_id)

    long_log_path = os.path.join(LONG_LOGS_DIR, f"{session_id}.yaml")
    try:
        write_yaml(long_log_path, long_log_yaml)
        print(f"  wrote {long_log_path}")
    except Exception as e:
        print(f"  ERROR writing long log: {e}")
        return False

    paths_to_commit = [os.path.relpath(long_log_path, LOG_REPO).replace("\\", "/")]

    # --- Call #2: project card update ---
    if project:
        existing = load_project_card(project)
        prior_logs = load_prior_long_logs(project, exclude_session_id=session_id)
        user_msg_pc = build_project_card_user_msg(project, existing, long_log_yaml, prior_logs)
        started = time.time()
        raw_pc = call_llm(PROJECT_CARD_SYSTEM, user_msg_pc, max_tokens=3072, temperature=0.2,
                          synth=True, timeout=120)
        pc_elapsed = int(time.time() - started)
        if raw_pc:
            card_yaml = strip_fences(raw_pc)
            card_yaml = sanitize_card(card_yaml, project, session_id)
            card_path = os.path.join(PROJECT_CARDS_DIR, f"{project}.yaml")
            try:
                write_yaml(card_path, card_yaml)
                print(f"  updated {card_path} (elapsed={pc_elapsed}s)")
                paths_to_commit.append(os.path.relpath(card_path, LOG_REPO).replace("\\", "/"))
            except Exception as e:
                print(f"  ERROR writing project card: {e}")
        else:
            print(f"  project-card API call failed (elapsed={pc_elapsed}s) — long log still written")

    # --- Commit + push per session (crash-resilient) ---
    git_commit_push(f"synth: {session_id[:8]} ({project or 'no-project'})", paths_to_commit)
    return True


def inject_synthesis_meta(long_log_yaml, elapsed, session_id):
    """Inject / override synthesis_meta fields. Kimi tends to copy the schema's
    placeholder model name verbatim ("kimi-k2.5") rather than use the actual
    model, so we always override `model` with the env-resolved value.
    """
    now_iso = datetime.now().astimezone().replace(microsecond=0).isoformat()
    actual_model = (
        os.environ.get("ACTIVITY_LOG_SYNTH_MODEL")
        or os.environ.get("ACTIVITY_LOG_MODEL")
        or ""
    )

    # Always override the model line if Kimi emitted one (avoids placeholder copy-paste)
    if actual_model and re.search(r"^\s+model:.*$", long_log_yaml, flags=re.MULTILINE):
        long_log_yaml = re.sub(
            r"^(\s+model:).*$",
            rf"\1 {actual_model}",
            long_log_yaml, count=1, flags=re.MULTILINE,
        )

    additions = []
    if "synthesized_at:" not in long_log_yaml:
        additions.append(f"  synthesized_at: {now_iso}")
    if "synthesis_duration_seconds:" not in long_log_yaml:
        additions.append(f"  synthesis_duration_seconds: {elapsed}")
    if "raw_transcript_file:" not in long_log_yaml:
        additions.append(f"  raw_transcript_file: {session_id}.md")
    if actual_model and "model:" not in long_log_yaml:
        additions.append(f"  model: {actual_model}")

    if not additions:
        return long_log_yaml
    if "synthesis_meta:" in long_log_yaml:
        return long_log_yaml.rstrip() + "\n" + "\n".join(additions) + "\n"
    return long_log_yaml.rstrip() + "\n\nsynthesis_meta:\n" + "\n".join(additions) + "\n"


# ---------- card sanitize pass ----------

def _project_recent_session_ids(project, current_session_id, n=STALE_SESSION_AGE):
    """Return set of the N most recent session_ids for this project, including
    the current one being synthesized. Based on long_logs mtime ordering.
    """
    ids = [current_session_id]
    if not os.path.isdir(LONG_LOGS_DIR):
        return set(ids)
    candidates = []
    for fname in os.listdir(LONG_LOGS_DIR):
        if not fname.endswith(".yaml"):
            continue
        sid = fname[:-5]
        if sid == current_session_id:
            continue
        path = os.path.join(LONG_LOGS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        if f"project: {project}" not in text:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, sid))
    candidates.sort(key=lambda x: x[0], reverse=True)
    ids.extend(sid for _, sid in candidates[: n - 1])
    return set(ids)


def _is_maintenance_artifact(path):
    if not path:
        return False
    base = os.path.basename(str(path).replace("\\", "/"))
    return base in MAINTENANCE_ARTIFACT_PATTERNS


def sanitize_card(card_yaml, project, current_session_id):
    """Post-Kimi mechanical pass. Returns the possibly-mutated YAML string.

    Operations (each logged in `quality_mechanical`):
      - scrub maintenance artifacts from artifacts_in_flight
      - drop threads / artifacts whose last session is older than STALE_SESSION_AGE
      - flag orphan ids referenced by superseded_constraints
      - flag token budget overruns and thread count that suggests a split
    """
    if not _HAS_YAML:
        return card_yaml
    try:
        data = yaml.safe_load(card_yaml)
    except Exception as e:
        print(f"  sanitize: yaml parse failed ({e}); writing Kimi output as-is")
        return card_yaml
    if not isinstance(data, dict):
        return card_yaml

    recent_ids = _project_recent_session_ids(project, current_session_id)

    scrubbed, stale_artifacts, stale_threads, orphan_ids = [], [], [], []

    artifacts = data.get("artifacts_in_flight") or []
    kept_artifacts = []
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        path = a.get("path", "")
        last = a.get("last_touched_session")
        if _is_maintenance_artifact(path):
            scrubbed.append(path)
            continue
        if last and last not in recent_ids:
            stale_artifacts.append(path)
            continue
        kept_artifacts.append(a)
    data["artifacts_in_flight"] = kept_artifacts

    threads = data.get("active_threads") or []
    kept_threads = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        last = t.get("last_active_session")
        tid = t.get("id", "")
        if last and last not in recent_ids:
            stale_threads.append(tid)
            continue
        kept_threads.append(t)
    data["active_threads"] = kept_threads

    constraints = data.get("constraints") or []
    known_ids = {c.get("id") for c in constraints if isinstance(c, dict)}
    for c in constraints:
        if not isinstance(c, dict):
            continue
        for ref in c.get("superseded_constraints") or []:
            if ref and ref not in known_ids:
                orphan_ids.append(ref)

    mutated = bool(scrubbed or stale_threads or stale_artifacts)
    token_estimate = len(card_yaml) // 4
    token_cap_exceeded = token_estimate > CARD_TOKEN_CAP
    thread_count = len(kept_threads)
    thread_over = thread_count > THREAD_SPLIT_THRESHOLD

    flags_present = (token_cap_exceeded or thread_over or orphan_ids
                     or stale_threads or stale_artifacts or scrubbed)

    if scrubbed or stale_threads or stale_artifacts or orphan_ids:
        print(f"  sanitize: scrubbed={scrubbed} stale_threads={stale_threads} "
              f"stale_artifacts={stale_artifacts} orphan_ids={orphan_ids}")
    if token_cap_exceeded:
        print(f"  sanitize: token_estimate={token_estimate} exceeds cap {CARD_TOKEN_CAP}")
    if thread_over:
        print(f"  sanitize: thread_count={thread_count} > split threshold {THREAD_SPLIT_THRESHOLD}")

    # No-op shortcut: if nothing changed AND nothing to flag, preserve Kimi's formatting.
    if not mutated and not flags_present:
        return card_yaml

    # Drop any Kimi-hallucinated quality_mechanical before we recompute.
    data.pop("quality_mechanical", None)

    if mutated:
        base_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                   default_flow_style=False, width=100)
    else:
        base_yaml = card_yaml.rstrip() + "\n"

    if flags_present:
        flags = {
            "quality_mechanical": {
                "token_estimate": token_estimate,
                "token_cap_exceeded": token_cap_exceeded,
                "thread_count": thread_count,
                "thread_count_over_split_threshold": thread_over,
                "orphan_ids": orphan_ids,
                "stale_threads_dropped": stale_threads,
                "stale_artifacts_dropped": stale_artifacts,
                "maintenance_artifacts_scrubbed": scrubbed,
            }
        }
        mech_yaml = yaml.safe_dump(flags, sort_keys=False, allow_unicode=True,
                                   default_flow_style=False, width=100)
        return base_yaml.rstrip() + "\n\n" + mech_yaml
    return base_yaml


# ---------- prune pass ----------

def prune_old_transcripts(days, dry_run=False):
    """Delete transcripts older than `days` that have a corresponding long log."""
    if not os.path.isdir(TRANSCRIPTS_DIR):
        return
    cutoff = time.time() - days * 86400
    removed = []
    for fname in os.listdir(TRANSCRIPTS_DIR):
        if not fname.endswith(".md") or fname == ".gitkeep":
            continue
        session_id = fname[:-3]
        transcript_path = os.path.join(TRANSCRIPTS_DIR, fname)
        long_log_path = os.path.join(LONG_LOGS_DIR, f"{session_id}.yaml")
        if not os.path.isfile(long_log_path):
            continue
        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError:
            continue
        if mtime > cutoff:
            continue
        if dry_run:
            print(f"[prune dry-run] would remove {transcript_path}")
            removed.append(transcript_path)
            continue
        try:
            os.remove(transcript_path)
            print(f"[prune] removed {transcript_path}")
            removed.append(transcript_path)
        except Exception as e:
            print(f"[prune] could not remove {transcript_path}: {e}")
    if removed and not dry_run:
        rels = [os.path.relpath(p, LOG_REPO).replace("\\", "/") for p in removed]
        git_commit_push(f"prune: {len(removed)} transcript(s) past {days}d retention", rels)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap how many sessions to synthesize this run")
    parser.add_argument("--idle-threshold", type=int, default=DEFAULT_IDLE_MINUTES,
                        help=f"Skip transcripts touched within N minutes (default {DEFAULT_IDLE_MINUTES}); avoids processing still-active sessions")
    parser.add_argument("--prune-older-than", type=int, default=DEFAULT_PRUNE_DAYS,
                        help=f"Delete transcripts older than N days if long log exists (default {DEFAULT_PRUNE_DAYS})")
    parser.add_argument("--no-prune", action="store_true",
                        help="Skip the prune pass")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write, delete, or push")
    args = parser.parse_args()

    if not args.dry_run:
        print("pulling latest...")
        git_pull()

    if not configured(synth=True) and not args.dry_run:
        print("synth API not configured (set ACTIVITY_LOG_SYNTH_* or ACTIVITY_LOG_* env vars)")
        return 0

    candidates = find_unsynthesized(idle_minutes=args.idle_threshold)
    if candidates:
        if args.limit:
            candidates = candidates[:args.limit]
        print(f"synthesizing {len(candidates)} session(s)")
        succeeded = 0
        for session_id, transcript_path, _ in candidates:
            if synthesize_one(session_id, transcript_path, dry_run=args.dry_run):
                succeeded += 1
        print(f"synthesis: {succeeded}/{len(candidates)} succeeded")
    else:
        print("nothing to synthesize")

    if not args.no_prune:
        prune_old_transcripts(args.prune_older_than, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
