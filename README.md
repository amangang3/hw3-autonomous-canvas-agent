# Canvas Agent

A deterministic Python guardrail for the autonomous HW3 Canvas forum agent. The code is built in this repository; deploy the directory as `~/hw3-agent` so paths in `RUNTIME.md` match.

## Requirements

- macOS or Linux
- `uv`
- Python 3.11+
- A Canvas token placed by the human, never by Hermes

## Install

```sh
cd ~/hw3-agent
uv sync --extra test
chmod 755 bin/canvas_tool scripts/collect_evidence.sh scripts/*.py
mkdir -p state/drafts logs
chmod 700 state state/drafts logs
```

The human creates the credential file outside the repository:

```sh
mkdir -p ~/.config/hw3-agent
chmod 700 ~/.config/hw3-agent
# Add exactly CANVAS_TOKEN=<new token> to ~/.config/hw3-agent/env
chmod 600 ~/.config/hw3-agent/env
```

Do not paste the token into a Hermes conversation. `canvas_tool` is the only component that reads it.

## Test and dry run

```sh
uv run --extra test pytest -q
bin/canvas_tool status
```

`status` is read-only. Before the credential exists it safely returns one JSON error object.

## Commands

```text
canvas_tool begin
canvas_tool new --run ID
canvas_tool thread --run ID --entry EID
canvas_tool post --run ID (--reply-to EID | --new-thread) --message-file PATH
canvas_tool skip --run ID --reason TEXT
canvas_tool end --run ID --outcome ok|error
canvas_tool status
```

Draft files must be regular UTF-8 files directly inside `state/drafts`; symlinks and paths elsewhere are refused. Every invocation writes one JSON object to stdout and one redacted JSONL audit record.

## Deploy your own agent

Review `ARCHITECTURE.md` and `RUNTIME.md`, then create an isolated Hermes profile. OAuth credentials are intentionally not copied between profiles, so authenticate the new profile separately:

```sh
hermes profile create canvasagent --clone-all \
  --description "Restricted autonomous Canvas discussion agent"
hermes -p canvasagent config set terminal.cwd "$HOME/hw3-agent"
hermes -p canvasagent config set security.redact_secrets true
hermes -p canvasagent auth add openai-codex --type oauth
```

Restrict the tools available to scheduled runs. Terminal remains enabled for `canvas_tool`; file access remains enabled for reading `RUNTIME.md` and writing drafts.

```sh
hermes -p canvasagent tools disable --platform cron \
  web browser code_execution vision image_gen tts skills todo memory \
  session_search connections clarify delegation cronjob computer_use
```

Install and start the long-lived gateway, then create the schedule. Replace the model/provider setup above if you use a different supported provider.

```sh
hermes gateway install
hermes gateway start
hermes -p canvasagent cron create '*/30 * * * *' \
  'Run one HW3 forum cycle. Follow ~/hw3-agent/RUNTIME.md exactly and actively find one substantive contribution to make.' \
  --name 'HW3 Canvas forum cycle' \
  --deliver local --failure-deliver local \
  --workdir "$HOME/hw3-agent" --pin
```

Verify the deployment:

```sh
hermes gateway status
hermes -p canvasagent cron list
hermes -p canvasagent tools list --platform cron
bin/canvas_tool status
```

Observe at least two unattended runs in `logs/agent.jsonl` before considering autonomy verified. These machine-level steps require the human's Canvas credential and model authentication; they are intentionally not automated by the repository.

## Evidence

```sh
scripts/collect_evidence.sh
```

The collector first runs a repository secret scan, then writes redacted run and verified-link evidence to `evidence/scheduled_runs.md`. Complete the live lost-ack exercise in `evidence/failure_recovery.md` after the course forum is available.
