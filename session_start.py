"""SessionStart hook.

Detects the current project from cwd, then loads up to three project cards and
injects them as `additionalContext`:

  1. project_cards/global.yaml              (if it exists — cross-project knowledge)
  2. project_cards/<parent_project>.yaml    (if the current card declares parent_project)
  3. project_cards/<project>.yaml           (the current project's card)

Each block gets a short header so Claude can tell them apart.

Fails open: missing card / unreadable / no project resolved → exits 0 without output.
"""
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_REPO = SCRIPT_DIR
PROJECT_CARDS_DIR = os.path.join(LOG_REPO, "project_cards")
GLOBAL_CARD_PATH = os.path.join(PROJECT_CARDS_DIR, "global.yaml")


def git_out(args, cwd):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip()
    except Exception:
        return ""


def resolve_project(cwd):
    if not cwd or not os.path.isdir(cwd):
        return ""
    top = git_out(["rev-parse", "--show-toplevel"], cwd)
    if not top:
        return ""
    override = os.path.join(top, ".claude", "project.yaml")
    if os.path.isfile(override):
        try:
            with open(override, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("project:"):
                        slug = line.split(":", 1)[1].strip().strip('"\'')
                        if slug:
                            return slug
        except Exception:
            pass
    return os.path.basename(top.rstrip("/").rstrip("\\"))


def read_card(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    return text if text.strip() else None


def extract_parent_project(card_text):
    """Cheap regex extract — avoids a pyyaml dependency in the hook path."""
    if not card_text:
        return ""
    m = re.search(r"^parent_project:\s*(.+)$", card_text, flags=re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip().strip('"\'')
    if val in ("", "null", "~"):
        return ""
    return val


def build_context_blocks(project):
    blocks = []

    global_text = read_card(GLOBAL_CARD_PATH)
    if global_text:
        blocks.append(
            "## Global project-card — cross-project knowledge (auto-loaded)\n\n"
            "Infrastructure-level dead ends, universal invariants, shared external "
            "dependencies. Human-maintained. Applies to every project.\n\n"
            f"```yaml\n{global_text}\n```"
        )

    card_path = os.path.join(PROJECT_CARDS_DIR, f"{project}.yaml")
    card_text = read_card(card_path)

    if card_text:
        parent = extract_parent_project(card_text)
        if parent:
            parent_path = os.path.join(PROJECT_CARDS_DIR, f"{parent}.yaml")
            parent_text = read_card(parent_path)
            if parent_text:
                blocks.append(
                    f"## Parent project-card ({parent}) — inherited by `{project}`\n\n"
                    "State from the parent project. The current project's card may "
                    "reference constraint/dead_end/invariant ids from this parent.\n\n"
                    f"```yaml\n{parent_text}\n```"
                )

        blocks.append(
            f"## Project card ({project}) — auto-loaded\n\n"
            f"Current state snapshot for `{project}`, synthesized by the "
            "activity-log Kimi pipeline. Use it to ground your understanding of "
            "where the project stands now: active threads, constraints, dead ends "
            "to avoid, work-in-flight artifacts.\n\n"
            f"```yaml\n{card_text}\n```"
        )

    return blocks


def main():
    # Best-effort pull (multi-machine consistency)
    try:
        subprocess.run(
            ["git", "pull", "--ff-only", "-q"],
            cwd=LOG_REPO, capture_output=True, timeout=5,
        )
    except Exception:
        pass

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    cwd = data.get("cwd") or os.getcwd()
    project = resolve_project(cwd)
    if not project:
        return 0

    blocks = build_context_blocks(project)
    if not blocks:
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(blocks),
        }
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
