# Skills

A skill is a directory with a `SKILL.md`: instructions, plus optional scripts
and references, that opencode advertises to the agent by its description and
loads only when a request calls for it. They are the right home for anything
too long or too specialised for [the system prompt](models.md#the-system-prompt)
— a runbook, a release process, how to query a particular system.

## Where the agent finds them

The image's `opencode.json` names one skills source, `/etc/mushroom/skills`.
opencode searches it for `SKILL.md` files at any depth, so it has two
subdirectories:

| Path in the pod | Comes from | Change it with |
|---|---|---|
| `/etc/mushroom/skills/builtin/` | `images/agent/skills/` in this repo | an image rebuild |
| `/etc/mushroom/skills/custom/` | `agent.skills` in the chart values | `helm upgrade` |

Both are listed under a single source, rather than as two sources, so that a
release with no `agent.skills` still points opencode at a directory that
exists.

A skill's ID is its directory name, not the `name` in its frontmatter, so
`custom/release-notes/SKILL.md` is the `release-notes` skill. Avoid giving a
builtin and a custom skill the same directory name.

## Writing one

```markdown
---
name: release-notes
description: Draft release notes from merged PRs. Use when someone asks what shipped.
---

1. List PRs merged since the most recent tag.
2. Group them by area and drop dependency bumps.
3. Reply with a Slack-formatted bulleted list, newest first.
```

`description` is what the model sees when deciding whether to load the skill,
so say *when* to use it, not just what it is. Without one the skill is not
advertised at all.

## Adding skills through Helm

```yaml
agent:
  skills:
    # A string is the SKILL.md body.
    release-notes: |
      ---
      description: Draft release notes from merged PRs. Use when asked what shipped.
      ---
      ...

    # A map adds supporting files beside SKILL.md.
    incident-triage:
      SKILL.md: |
        ---
        description: Triage a production alert. Use when someone pastes an alert.
        ---
        Run `sh scripts/recent-deploys.sh` first, then ...
      scripts/recent-deploys.sh: |
        #!/bin/sh
        ...
```

The chart renders these into a ConfigMap mounted at
`/etc/mushroom/skills/custom`, and the agent pods roll when the set changes.
It refuses to render when a skill name is not lowercase-and-hyphens, when a
map has no `SKILL.md`, or when a file path is absolute or contains `..`.

Things to know:

- **Mounted files are not executable.** Have `SKILL.md` run scripts as
  `sh scripts/x.sh`, not `./scripts/x.sh`.
- **ConfigMaps are capped at 1 MiB** in total across all skills. Bigger
  reference material belongs in the image.
- **Values are not secret.** A skill that needs a credential should read it
  from an environment variable supplied through the chart's Secret, not
  embed it.

## Adding skills to the image

Put a directory with a `SKILL.md` under `images/agent/skills/` and rebuild.
Use this for skills that ship with Mushroom itself or that carry large or
binary files; scripts keep the executable bit they have in git.

## Local development

The compose stack mounts `deploy/local/skills/` at the same `custom/` path.
Drop a skill directory there, or point elsewhere:

```bash
SKILLS_DIR=~/src/team-skills ./scripts/dev.sh up
```

## Checking what the agent sees

opencode's server lists the skills it found. Discovery runs in the background
after the server starts, so start one and give it a few seconds:

```bash
kubectl -n mushroom exec deploy/mushroom-agent -- env HOME=/workspace sh -c '
  opencode service start </dev/null >/dev/null 2>&1 & sleep 10
  opencode api GET /api/skill </dev/null'
```

`HOME` must be the workspace, where the entrypoint put opencode's config; an
exec'd shell does not inherit the entrypoint's export.

Expect your skills with paths under `/etc/mushroom/skills/`, plus opencode's
own bundled `opencode` and `report` skills.

## Permissions

The agent runs with `session.permissions: autoaccept` (see
`images/agent/cli.json`), so any skill it finds it may load without a prompt.
Everything under the skills source is therefore effectively part of the
agent's instructions; review skills with the same care as the system prompt.
