---
name: pm-loop-tune
description: Show or change pm-loop knobs (models, effort, tool-call caps, review routing, merge policy, local LLM endpoint) by editing ~/.config/pm-loop/config.toml or <repo>/.pm-loop.toml. Use when asked to tune, cheapen, or reroute the loop.
user-invocable: true
argument-hint: "[what to change]"
allowed-tools: Bash(pm config *), Read, Edit, Write
effort: low
---
# Tune pm-loop

Request: `$ARGUMENTS`

## Current effective config
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm config`

Config layers (later wins): plugin defaults → `~/.config/pm-loop/config.toml` → `<repo>/.pm-loop.toml` → `PM_LOOP__section__key` env vars.
Change the smallest layer that fits (repo file for repo-specific routing; user file for models/bot/llm). Show the diff you made and the resulting `pm config <section>`.
Knobs: `roles.<role>.{model,effort,max_turns,max_tool_calls}`, `review.{classes,tier_by_class,adversarial_paths,required_verdict}`, `merge.{method,required_check,base,forbid_trailers}`, `status.{prose,max_prose_words}`, `llm.{endpoint,model,timeout_s}`, `poll.interval_s`, `watch.timeout_s`, `lanes.{deny_subagents,deny_background}`.
