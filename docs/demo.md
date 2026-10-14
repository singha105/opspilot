# Demo scripts

Both demos run on an 8 GB laptop. Before an audience, run the incident once beforehand so
the models are downloaded and loaded (the first triage after a cold start takes longer).

```bash
make demo                                   # replay: no cluster
make demo-live SCENARIO=oom-payments        # live: cluster, injected fault, webhook alert
```

## 60-second demo (replay)

| Time | Show | Say |
|---|---|---|
| 0:00 | Console, Incidents page, the blue banner | "OpsPilot is an incident agent for Kubernetes. Its cluster access is read-only; nothing changes until a person approves." |
| 0:08 | `oom-payments` selected, press **Investigate** | "This replays a recorded outage: payments-api is crash-looping." |
| 0:12 | The progress box: triage, retrieve, tool calls | "It triages the alert, pulls runbooks with hybrid search, then calls read-only tools one at a time." |
| 0:30 | Evidence list, then **Diagnosis** | "OOMKilled, confidence 0.9. Every claim cites evidence or a runbook chunk; click [E2] and you land on it." |
| 0:42 | **Proposed action**: memory 128Mi to 512Mi, rollback plan | "The fix comes from an allowlist, with a dry-run diff and a rollback plan." |
| 0:50 | Type a name, **Approve**; Outcome and report download | "Approval mints a single-use token for exactly this change. Replay simulates it; live, it executes and verifies recovery." |
| 0:58 | Evals page headline | "On 20 held-out faults: 12 of 20 root causes right, zero injected instructions followed." |

## 5-minute demo (live)

1. **Setup (before):** `make demo-live SCENARIO=oom-payments`. The cluster comes up, the
   fault is injected, and the alert is posted to the webhook; the API investigates in the
   background.
2. **The problem (0:00-0:40).** `make fault-status`: payments-api is OOMKilled and its
   caller orders-api is not ready. "An alert just fired; it says pods are restarting, not
   why."
3. **The webhook (0:40-1:00).** Console, Incidents, live mode, "Received by the webhook",
   open the incident. Point at the banner: read-only access, gated writes.
4. **Investigation (1:00-2:00).** Triage; retrieved chunks (open one: it is the real
   runbook text); evidence, from `list_pods` to `describe_pod` with `OOMKilled exit 137` and
   `limits memory=128Mi`. "It chose these calls itself, at most eight, never a write."
5. **Diagnosis (2:00-2:40).** Category, component, confidence bar, the cited summary and the
   alternatives it ruled out. Mention the guards: missing citations are repaired once, then
   escalated; a diagnosis the pods contradict escalates too.
6. **Proposal and approval (2:40-3:30).** The dry-run diff from the actions server, risk
   notes, rollback plan. Approve with your name. "The token is bound to this action's
   hash, lives ten minutes and works once; the model never sees it."
7. **Recovery (3:30-4:10).** Outcome: executed, verification `resolved`. Run
   `make fault-status`: every Deployment ready. Download the report.
8. **How we know it works (4:10-5:00).** Evals page: headline table with confidence
   intervals, the ablation (retrieval did not measurably help at n=20), the safety table,
   the live subset. "Every miss is classified in the report; most are the 4B model
   ignoring its own evidence."

Afterwards: `uv run opspilot faults reset oom-payments` and `make down-all`.

## Recording the README GIF (60 seconds)

The current `docs/assets/demo.gif` was assembled from 16 console frames of a real replay
run (Investigate, progress, evidence, diagnosis, proposal, approval, outcome, evals), each
with a caption, because screen recording was not available on the machine that made it.
To replace it with a screen recording:

1. `make demo`, open http://localhost:8501 in a 1280x800 browser window, light theme,
   zoom 100%. Close other tabs and notifications.
2. Warm up: run `oom-payments` once and approve it, so the model is loaded. Reload the page.
3. Start a screen recording of the browser window only (macOS: Shift-Cmd-5, "Record
   Selected Portion").
4. Record, following the 60-second script above: Investigate, wait on the progress box,
   scroll slowly through Evidence and Diagnosis, click `[E2]`, scroll to the proposal,
   type a name, Approve, show the Outcome. Speed up the waiting in editing rather than
   cutting it, so viewers see the agent work.
5. Convert to a GIF under 10 MB, 960 px wide, 12 fps:

   ```bash
   ffmpeg -i demo.mov -vf "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" docs/assets/demo.gif
   ```

6. Commit the new `docs/assets/demo.gif`; the README already points at it.
