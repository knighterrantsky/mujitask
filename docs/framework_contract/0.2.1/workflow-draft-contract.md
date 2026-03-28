# Workflow Draft Contract

## 1. Purpose

`workflow_draft.yaml` is the review-only artifact generated from `raw_trace.json`.

It is designed to preserve two layers at the same time:

- evidence from the recording
- higher-level business semantics inferred from the trace

Current execution boundary:

- `workflow_draft.yaml` is not executable
- `workflow.yaml` will remain the future executable contract
- the current runtime only generates and serves `workflow_draft`

## 2. Top-Level Structure

`workflow_draft` currently has four top-level blocks:

- `source`
- `variables`
- `steps`
- `review`

### `source`

Captures where the draft came from:

- `recording_id`
- `trace_path`
- `provider`
- `target_key`
- `recorded_at.started_at`
- `recorded_at.finished_at`

### `variables`

Represents values that matter to downstream business steps.

Current supported kinds:

- `extracted`
- `derived`
- `runtime_input`
- `sink_output`

Each variable can declare:

- `required`
- `source_step_id`
- `source_event_refs`
- `value_hint`
- `type_hint`
- `constraints`
- `failure_code`
- `failure_message_template`

### `steps`

`workflow_draft` uses linear steps plus explicit gating metadata.

Each step can include:

- `step_id`
- `name`
- `intent`
- `action_type`
- `tab_id`
- `page_url`
- `event_refs`
- `inputs`
- `locator_candidates`
- `required_inputs`
- `emits`
- `guards`
- `candidate_checks`
- `effects`
- `artifacts`
- `on_fail`
- optional `sink`
- optional `review_status`

### `review`

Holds compiler output that helps a human reviewer understand quality and risk:

- `generated_at`
- `summary`
- `warnings`
- `known_issues`
- `dropped_event_sequences`
- `blockers`

## 3. Evidence Model

Every semantic step must remain traceable back to the source recording.

Current evidence references:

- `event_refs.sequences`
- `event_refs.event_ids`
- `tab_id`
- artifact file paths already captured during recording

This is what lets a reviewer ask:

- which raw events produced this step
- which tab it happened in
- which screenshot/html/dom files should be inspected

## 4. Popup Guard Semantics

Blocking popups are not modeled as normal business steps.

They are represented through:

- `review.blockers`
- step-level `guards`

Current guard contract:

- `kind = popup_absent`
- `mode = manual_dom_clear`
- `timeout_s = 10`
- `poll_interval_ms = 300`
- `success_condition = popup_not_detected_for_2_consecutive_polls`
- `failure_code = BLOCKING_POPUP_TIMEOUT`

Current intended runtime semantics:

- when a blocking popup appears, the step enters manual intervention waiting
- the system only watches DOM state; it does not require a separate confirm API
- the human can dismiss the popup directly in the browser
- if the popup disappears and stays absent for two consecutive polls, the step may continue
- if the popup remains present at the 10 second deadline, the step fails
- repeated popup appearance inside the same waiting window does not reset the timer

Blocker evidence should preserve:

- screenshot
- html snapshot
- dom summary
- popup text excerpt
- `tab_id`
- `page_url`

## 5. External Sink Semantics

External writes remain normal steps in the draft, not a separate side channel.

They use a `sink` block with:

- `sink_type`
- `required`
- `result_mode`
- `success_condition`
- `failure_condition`
- `timeout_s`
- `retry_policy`

Current result modes:

- `sync`
- `async_poll`
- `none`

Current rules:

- `required=true` means failure blocks the workflow
- `required=false` means failure is recorded but the flow may continue
- `result_mode=async_poll` means the sink expects polling semantics
- `result_mode=none` is review-only and should stay non-blocking in MVP

## 6. Current Compiler Output

The current compiler is rule-first.

It already does these transformations:

- normalize duplicate navigation and incremental input events
- detect blocking popup evidence
- keep `tab.open`, `tab.switch`, `tab.close` evidence
- infer Amazon product extraction variables when a detail page artifact exists
- infer `jd_search_query` from extracted Amazon product fields
- insert `prepare_external_row_payload` before an external sink step
- classify unobservable sinks as optional

## 7. Naming Rules

Keep these names stable:

- `raw_trace.json`
- `workflow_draft.yaml`
- `workflow.yaml`

Do not reintroduce `candidate_flow.yaml` as the primary draft artifact name.

## 8. Current Non-Goals

This contract does not yet define:

- executable `workflow.yaml`
- loader semantics
- action DSL
- browser check DSL
- replay bundle
- codegen or ReAct behavior

Those stay in later iterations.
