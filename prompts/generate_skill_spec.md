# Generate Skill Spec Prompt

Use this prompt when adding or updating a Mujitask agent skill.

You are editing a Mujitask repository. Do not hand-write `SKILL.md`. Create or update `skills/{skill_code}/skill.spec.yaml` as the source of truth, then run:

```bash
uv run --extra dev python tools/render_skill.py
uv run --extra dev python tools/validate_skill.py
```

The generated `SKILL.md` must be committed with the spec.

## Required Context

Read these files before changing a skill:

- `contracts/skill_contract.md`
- `contracts/skill_spec.schema.json`
- Existing `skills/{skill_code}/skill.spec.yaml`, if present
- Existing wrapper scripts under `skills/{skill_code}/`
- Related workflow contracts under `contracts/workflow/**`
- Related field/state contracts under `contracts/fields/**` and `contracts/states/**`

## Spec Requirements

Fill every required field:

- `metadata`: skill name, title, short description, owner, and whether the skill has side effects.
- `intents`: one entry per route. Each intent needs trigger examples, negative examples, command, task name, side effects, and input contract.
- `input_extraction`: only fields the agent may extract from user text.
- `fixed_config`: environment/config keys loaded by the wrapper; never include secret values.
- `output_contract`: fixed first reply and final delivery rules.
- `failure_handling`: user-visible behavior when input, submit, external API, or async execution fails.

For a side-effect skill, include explicit negative examples for confusing cases such as similarly named tables or commands. Do not rely on broad natural-language wording when the route writes Feishu, submits runtime tasks, calls external systems, or sends notifications.

## Eval Examples

Update `skills/{skill_code}/examples.eval.yaml` with:

- Positive examples for every intent.
- Negative or ambiguous examples for easy misroutes.
- Expected command and task name.
- `must_not_include` strings for risky wrong routes.

## Final Rule

If `tools/validate_skill.py` fails, the skill contract is not satisfied and the task cannot be claimed complete.
