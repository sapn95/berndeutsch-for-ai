#!/usr/bin/env python3
"""Link the hook and the dictionary lookup into the Claude config dir, then
print the settings block to merge.

settings.json is deliberately not written. It is the user's file, it usually
already has hooks in it, and a script that rewrites it is a script that
eventually eats somebody's config.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def config_dir():
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(explicit) if explicit else Path.home() / ".claude"


def link(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.symlink_to(src)
    return dst


def probe(hook, prompt):
    # No session_id on purpose. The hook only serves the full rulebook once per
    # session, so a fixed probe id would pass on a fresh machine and then fail
    # on every reinstall because the state file already exists.
    payload = json.dumps({"prompt": prompt})
    result = subprocess.run(
        [sys.executable, str(hook)], input=payload, capture_output=True, text=True
    )
    return result


def main():
    cfg = config_dir()
    hook = REPO / "hooks" / "berndeutsch_gate.py"
    bdw = REPO / "scripts" / "bdw"
    for path in (hook, bdw):
        path.chmod(path.stat().st_mode | 0o111)

    print("linked:")
    for src, dst in ((hook, cfg / "hooks" / hook.name), (bdw, cfg / "scripts" / "bdw")):
        link(src, dst)
        print(f"  {dst} -> {src}")

    print()
    # Three assertions, not one. Checking only that additionalContext exists
    # would pass even when no rulebook file was found and the payload is an
    # empty shell, which is exactly the install worth catching.
    fired = probe(hook, "Chasch mer säge öb das itz guet isch, gäu?")
    if fired.returncode != 0 or not fired.stdout.strip():
        print("self-test FAILED: the hook produced nothing on a Bärndütsch prompt", file=sys.stderr)
        return 1
    context = json.loads(fired.stdout)["hookSpecificOutput"]["additionalContext"]
    if "Bärndütschi Schrybwys" not in context:
        print("self-test FAILED: the rulebook was not found, only a stub was injected", file=sys.stderr)
        print(f"  expected {REPO / 'rules' / 'schrybwys.md'} to be readable", file=sys.stderr)
        return 1
    print(f"self-test: fires on Bärndütsch and injects the rulebook ({len(context)} chars)")

    silent = probe(hook, "Run git status and then git commit -m 'fix'")
    if silent.stdout.strip():
        print("self-test FAILED: the hook fired on a plain English prompt", file=sys.stderr)
        return 1
    print("self-test: stays silent on English")

    block = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            # "python3" from PATH, not sys.executable: the
                            # installer may well be running inside a virtualenv
                            # that the hook has no business depending on. The
                            # hook is stdlib-only, so any python3 will do.
                            "command": "python3",
                            "args": [str(cfg / "hooks" / hook.name)],
                            "timeout": 10,
                            "statusMessage": "Bärndütsch-Schrybwys lade…",
                        }
                    ]
                }
            ]
        }
    }
    print(f"\nNow merge this into {cfg / 'settings.json'}, keeping any hooks already there:\n")
    print("\n".join("  " + line for line in json.dumps(block, indent=2, ensure_ascii=False).splitlines()))
    print(
        "\nThe exec form (`command` + `args`) runs the hook directly with no shell"
        "\nin between, so nothing re-interprets the path."
        "\n\nThen open /hooks once in Claude Code, or restart it, so the new config is read."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
