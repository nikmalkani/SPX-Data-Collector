# Agents Notes

## Instruction Sources Used In This Repo
- Runtime/system instructions from Codex environment.
- Workspace-level instructions provided via `AGENTS.md` context in this session.
- Direct user requirements in the active task thread.

## UI Tabs Not Loading: Root Cause + Prevention

### What failed
- `Options Analyzer` and `SQL Lab` did not load because inline JS in `_HTML` failed to parse.
- Browser showed: `Uncaught SyntaxError: Invalid or unexpected token (index):503`.
- The syntax error prevented tab init/bind code from running at all.

### Why it happened
- The UI is embedded in Python as a triple-quoted string (`_HTML` in `backtest_ui.py`).
- JS escape sequences like `"\n"` inside that Python string can become literal newlines in served JS if not escaped correctly for this embedding context.
- Some modern JS constructs can also cause compatibility/parser issues depending on browser/runtime.

### Fixes applied
- Replaced brittle JS usages in the inline script:
  - Removed nullish-coalescing (`??`) in key paths.
  - Replaced `replaceAll(...)` with `split(...).join(...)`.
- Corrected escaped newlines in JS string literals:
  - Use `"\\n"` in the Python-embedded JS where literal `\n` is intended.

### Operational gotcha
- Hard refresh is not enough after editing `_HTML` in Python.
- You must restart the running `backtest_ui.py`/`spx-backtest-ui` process to serve updated HTML/JS.

### Quick troubleshooting checklist
1. Open browser console and check first syntax error line in `(index)`.
2. Map that line to `_HTML` line numbers in `src/spx_collector/backtest_ui.py`.
3. Check for Python-string escape interactions in inline JS (`\n`, `\t`, etc.).
4. If parse error exists, assume tab code never initialized; fix parse error first.
5. Restart backend process after each `_HTML` edit before re-testing.
6. Ignore `favicon.ico 404`; it is unrelated.

## Lightsail Access Shortcuts
- If the user says anything like:
  - "log me into my server"
  - "ssh me into my lightsail server"
  - "ssh me into my server"
  - or equivalent phrasing to open their server workspace
- Open VS Code Remote-SSH in a new window using:

```bash
code --new-window --remote ssh-remote+lightsail-spx /home/ubuntu/SPX-Data-Collector
```

- If the user explicitly asks for terminal-only SSH (for example: "ssh in terminal only", "plain ssh session"), run:

```bash
ssh -i ~/Downloads/LightsailDefaultKey-us-west-2.pem ubuntu@16.144.246.185
```
