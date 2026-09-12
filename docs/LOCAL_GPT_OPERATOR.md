# Local GPT Operator

MON EDIT can auto-dispatch every `WAITING_GPT` request through a local computer-use operator instead of requiring the human to click **MỞ CHATGPT → COPY YÊU CẦU** for every footage batch.

Architecture remains GPT-only:

```text
Moon -> bounded Drive request/evidence -> Local Operator -> ChatGPT -> response.json -> Moon
```

The Local Operator is **hands only**. It must not analyze footage, generate semantic JSON itself, or replace GPT. Its only action is to operate the user's already-authenticated ChatGPT UI and submit the exact handoff instruction once.

## Runtime backend

The first backend is Open Interpreter. MON EDIT calls it non-interactively with `interpreter exec`. The prompt explicitly tells Interpreter to use computer-use/playwright, operate the existing signed-in ChatGPT browser session, submit one handoff message, and stop immediately.

If `interpreter` is unavailable or dispatch fails, Moon does not fail the pipeline. It keeps the current Drive request active and falls back to the existing manual **MỞ CHATGPT / COPY YÊU CẦU** path.

## Install on Windows

Open PowerShell and install Open Interpreter using its official installer, then restart the terminal and verify:

```powershell
irm https://www.openinterpreter.com/install.ps1 | iex
interpreter --version
```

Configure an Interpreter profile capable of computer use. The profile may use a local model (for example through Ollama or LM Studio) or another provider. MON EDIT does not depend on the local model for video reasoning; the model only needs to reliably operate the browser UI.

## Project configuration

Optional file: `<project>/.moon/operator.json`

```json
{
  "chatgpt_url": "https://chatgpt.com/",
  "auto_chatgpt": {
    "enabled": true,
    "executable": "interpreter",
    "profile": "desktop-local",
    "timeout_seconds": 180,
    "max_dispatch_attempts": 2
  }
}
```

Environment overrides:

- `MOON_OPERATOR_AUTO_CHATGPT=0|1`
- `MOON_OPERATOR_INTERPRETER=<path-or-command>`
- `MOON_OPERATOR_INTERPRETER_PROFILE=<profile>`

On Windows, auto dispatch defaults to enabled. If Interpreter is not installed, the system remains manual without losing the active request.

## Invalid GPT response handling

A malformed/stale `response.json` no longer needs to send the operator workflow directly to `FAILED` when the auto operator is active.

Moon keeps the active request, records the validation error, and asks the Local Operator to send a narrow repair request back to GPT. The repair instruction includes the validator error and explicitly says to keep valid semantic work rather than re-analyzing evidence unnecessarily. After the configured retry limit, Moon remains waiting for manual correction instead of advancing with invalid data.

## Safety boundary

The Local Operator instruction prohibits entering credentials or switching accounts. The user must already be signed in to ChatGPT. The operator may submit only the generated MON EDIT handoff message and must stop immediately after submission.
