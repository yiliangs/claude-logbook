"""Stop hook.

Per turn, this hook does THREE things:

1. Append the assistant's text + a brief tool-use trace to the per-session
   raw transcript: transcripts/<session_id>.md

2. Extract the list of file paths touched by tool use (artifacts).

3. Call the small LLM (per ACTIVITY_LOG_* env vars) to fill `question_summary`
   and `response_core` on the most recent short-log entry, plus write `artifacts`.

Fails open: missing env vars, network/API error, or bad parse → exits 0,
the entry keeps its mechanical fields and placeholder AI fields.
"""
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from _api import call_llm, configured  # noqa: E402

LOG_REPO = os.path.dirname(SCRIPT_DIR)
SHORT_LOG_DIR = os.path.join(LOG_REPO, "short_log")
TRANSCRIPTS_DIR = os.path.join(LOG_REPO, "transcripts")

MAX_INPUT_CHARS = 16000
MIN_RESPONSE_CHARS = 200

ARTIFACT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def extract_turn(transcript_path):
    """Return (last_user_text, last_assistant_text, artifact_paths) from final turn."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None, None, [], []

    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None, None, [], []

    assistant_parts = []
    artifacts = []
    last_user_text = None
    tool_use_trace = []

    for line in reversed(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        msg = d.get("message", {})
        c = msg.get("content") if isinstance(msg, dict) else None

        if t == "user":
            user_text = None
            if isinstance(c, str):
                user_text = c
            elif isinstance(c, list):
                texts = [b.get("text", "") for b in c if b.get("type") == "text"]
                if texts:
                    user_text = "\n".join(texts)
            if user_text is None:
                continue
            last_user_text = user_text
            break

        if t == "assistant" and isinstance(c, list):
            for b in c:
                btype = b.get("type")
                if btype == "text":
                    assistant_parts.append(b.get("text", ""))
                elif btype == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {}) or {}
                    if name in ARTIFACT_TOOLS:
                        fp = inp.get("file_path")
                        if fp and fp not in artifacts:
                            artifacts.append(fp)
                    tool_use_trace.append((name, inp))

    assistant_text = "\n".join(reversed(assistant_parts)).strip()
    return last_user_text, assistant_text, list(reversed(artifacts)), list(reversed(tool_use_trace))


def append_transcript(session_id, turn, ts, assistant_text, tool_use_trace):
    if not session_id:
        return
    path = os.path.join(TRANSCRIPTS_DIR, f"{session_id}.md")
    if not os.path.isfile(path):
        return  # transcript was never opened (no UserPromptSubmit ran)

    block = [f"\n## Turn {turn} [{ts}] — Assistant\n", assistant_text or "(no text content)"]
    if tool_use_trace:
        block.append("\n\n_Tool calls:_")
        for name, inp in tool_use_trace[:30]:
            short = json.dumps(inp, ensure_ascii=False)[:200]
            block.append(f"\n- `{name}` {short}")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
    except Exception:
        pass


SHORT_SYSTEM = (
    "You compress a developer conversation turn into two declarative fields "
    "for a search/retrieval index. The fields are NOT a narrative; they are "
    "extraction surfaces for later querying. Follow the tone and output rules "
    "in the user message. Output a raw JSON object only — no markdown fencing, "
    "no preamble."
)


def _load_schema(name):
    path = os.path.join(LOG_REPO, "schemas", name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def summarize(prompt_text, response_text):
    p = (prompt_text or "")[:MAX_INPUT_CHARS // 4]
    r = (response_text or "")[:MAX_INPUT_CHARS - len(p)]
    tone = _load_schema("tone.md")
    output_rules = _load_schema("output_rules.md")

    user_msg = (
        f"# OUTPUT RULES (JSON output, not YAML — but the null and identifier rules still apply)\n\n{output_rules}\n\n"
        f"# TONE\n\n{tone}\n\n"
        f"# TASK\n\nOutput a JSON object with two fields:\n"
        "- `question_summary`: what was asked, one sentence, declarative\n"
        "- `response_core`: the key insight, decision, or solution, 1-2 sentences\n\n"
        f"# USER PROMPT\n\n{p}\n\n# ASSISTANT RESPONSE\n\n{r}\n"
    )

    raw = call_llm(SHORT_SYSTEM, user_msg, max_tokens=300, temperature=0.1)
    if not raw:
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict):
        return None
    if "question_summary" not in result or "response_core" not in result:
        return None
    return result


def patch_last_log_line(session_id, ai_fields, artifacts):
    """Update the most recent entry for this session with AI fields + artifacts."""
    shard = os.path.join(SHORT_LOG_DIR, time.strftime("%Y-%m") + ".jsonl")
    if not os.path.isfile(shard):
        return

    try:
        with open(shard, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    if not lines:
        return

    # Find the most recent line matching this session_id
    target_idx = None
    for i in range(len(lines) - 1, -1, -1):
        try:
            e = json.loads(lines[i])
        except Exception:
            continue
        if e.get("session_id") == session_id:
            target_idx = i
            break
    if target_idx is None:
        return

    try:
        entry = json.loads(lines[target_idx])
    except Exception:
        return
    if entry.get("question_summary"):
        return  # already filled, idempotent

    if ai_fields:
        entry.update(ai_fields)
    if artifacts:
        entry["artifacts"] = artifacts

    lines[target_idx] = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with open(shard, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        return


def get_turn_for_session(session_id):
    """Return the latest turn number recorded for this session in current shard."""
    shard = os.path.join(SHORT_LOG_DIR, time.strftime("%Y-%m") + ".jsonl")
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
                    n = max(n, e.get("turn", 0))
    except Exception:
        return 1
    return n or 1


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    transcript_path = data.get("transcript_path")
    session_id = data.get("session_id", "") or ""

    user_text, assistant_text, artifacts, tool_use_trace = extract_turn(transcript_path)

    # Always append assistant text to per-session transcript (mechanical, no API)
    if assistant_text or tool_use_trace:
        from datetime import datetime
        ts = datetime.now().astimezone().replace(microsecond=0).isoformat()
        turn = get_turn_for_session(session_id)
        append_transcript(session_id, turn, ts, assistant_text, tool_use_trace)

    # AI fields require an LLM call — fail open if unconfigured
    if not configured():
        if artifacts:
            patch_last_log_line(session_id, None, artifacts)
        return 0

    if not assistant_text or len(assistant_text) < MIN_RESPONSE_CHARS:
        # Trivial response — still record artifacts but skip AI summarization
        if artifacts:
            patch_last_log_line(session_id, None, artifacts)
        return 0

    ai_fields = summarize(user_text, assistant_text)
    patch_last_log_line(session_id, ai_fields, artifacts)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
