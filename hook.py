"""UserPromptSubmit hook.

Per turn, this hook does TWO things:

1. Append the user prompt to the per-session raw transcript:
       transcripts/<session_id>.md
   (The Stop hook appends the assistant's reply to the same file.)

2. Write a placeholder short-log entry to short_log/YYYY-MM.jsonl with the
   mechanical fields. The Stop hook patches in the AI fields (question_summary,
   response_core), the artifacts list, and the status.

Skips logging when the prompt starts with /remind (avoids circular noise when
/remind itself queries the log).
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_REPO = SCRIPT_DIR
SHORT_LOG_DIR = os.path.join(LOG_REPO, "short_log")
TRANSCRIPTS_DIR = os.path.join(LOG_REPO, "transcripts")


def git_out(args, cwd):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip()
    except Exception:
        return ""


def resolve_project(cwd):
    """Return project slug. Default = git repo basename. Override = .claude/project.yaml."""
    if not cwd or not os.path.isdir(cwd):
        return ""

    top = git_out(["rev-parse", "--show-toplevel"], cwd)
    if not top:
        return ""

    # Override: .claude/project.yaml with `project: <slug>`
    override_path = os.path.join(top, ".claude", "project.yaml")
    if os.path.isfile(override_path):
        try:
            with open(override_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("project:"):
                        slug = line.split(":", 1)[1].strip().strip('"\'')
                        if slug:
                            return slug
        except Exception:
            pass

    return os.path.basename(top.rstrip("/").rstrip("\\"))


def count_turn(session_id, target_date):
    """Return 1-indexed turn number for this session in the current month shard."""
    shard = os.path.join(SHORT_LOG_DIR, target_date.strftime("%Y-%m") + ".jsonl")
    if not os.path.isfile(shard):
        return 1
    n = 0
    try:
        with open(shard, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("session_id") == session_id:
                    n += 1
    except Exception:
        return 1
    return n + 1


def append_transcript(session_id, turn, ts, prompt):
    """Append the user prompt to transcripts/<session_id>.md."""
    if not session_id:
        return
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPTS_DIR, f"{session_id}.md")
    block = f"\n---\n## Turn {turn} [{ts}] — User\n\n{prompt}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception:
        pass


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    prompt = data.get("prompt", "") or ""
    if prompt.lstrip().startswith("/remind"):
        return 0

    session_id = data.get("session_id", "") or ""
    cwd = data.get("cwd") or os.getcwd()

    project = resolve_project(cwd)
    branch = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd) if os.path.isdir(cwd) else ""

    # Best-effort pull (multi-machine consistency)
    try:
        subprocess.run(
            ["git", "pull", "--ff-only", "-q"],
            cwd=LOG_REPO, capture_output=True, timeout=5,
        )
    except Exception:
        pass

    now = datetime.now().astimezone().replace(microsecond=0)
    ts = now.isoformat()
    turn = count_turn(session_id, now.date())

    # 1. Append raw prompt to per-session transcript
    append_transcript(session_id, turn, ts, prompt)

    # 2. Write placeholder short-log entry — AI fields filled by Stop hook
    entry = {
        "session_id": session_id,
        "turn": turn,
        "timestamp": ts,
        "machine": socket.gethostname(),
        "project": project,
        "git_branch": branch,
        "thread_id": None,                 # synthesizer-assigned later, propagated via project_card
        # AI fields (Stop hook fills these):
        "question_summary": None,
        "response_core": None,
        # Mechanical / derived (Stop hook fills these):
        "artifacts": [],
        "status": "resolved",              # default; long-log synthesis can recompute
    }

    os.makedirs(SHORT_LOG_DIR, exist_ok=True)
    shard = os.path.join(SHORT_LOG_DIR, now.strftime("%Y-%m") + ".jsonl")
    try:
        with open(shard, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
