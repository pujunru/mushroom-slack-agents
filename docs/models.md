# Model provider

Mushroom's agents run [opencode](https://opencode.ai), which talks to a model
provider. Which provider is configuration, not code.

## Configuring it

Two settings, which must agree:

```yaml
agent:
  # `provider/model`. The prefix before the first `/` selects the backend;
  # everything after it is the provider's own model id and may contain
  # further slashes.
  model: "openrouter/anthropic/claude-sonnet-4.5"
  # The environment variable opencode reads the credential from. It is
  # provider-specific, so it must match the prefix above.
  modelApiKeyEnv: "OPENROUTER_API_KEY"
```

The credential itself comes from `MODEL_API_KEY` (or Vault), and the worker
exports it under `modelApiKeyEnv` before invoking opencode.

That indirection is the whole mechanism. opencode v2 reads a provider-specific
variable from the process serving model requests — `OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and so on — so the only thing that has to
change when you switch providers is which name the key is exported under.

`--standalone` matters here: it runs a private foreground server for the
invocation, which is what gets this process's environment to the process that
actually makes model requests. Without it the credential never arrives.

## Why OpenRouter by default

One key reaches many vendors, so trying a different model is a one-line values
change rather than a new account and a new secret. The trade-offs are below;
switch to a direct provider if they bite.

## Choosing a model

Any OpenRouter slug works, prefixed with `openrouter/`:

| Use | Model | $/M in → out |
|---|---|---|
| Strong coding/agentic | `openrouter/anthropic/claude-sonnet-4.5` | 3.00 → 15.00 |
| Strongest | `openrouter/anthropic/claude-opus-4.5` | 5.00 → 25.00 |
| Cheap, still tool-capable | `openrouter/anthropic/claude-haiku-4.5` | 1.00 → 5.00 |
| Very cheap open-weight | `openrouter/deepseek/deepseek-v3.2` | 0.28 → 0.42 |

**Check tool-calling support before adding a slug.** An agent needs it, and it
is per-model rather than universal on OpenRouter — some models lack it
entirely, others only on certain upstream providers:

```bash
curl -s https://openrouter.ai/api/v1/models \
  | jq -r '.data[] | select(.supported_parameters[]? == "tools") | .id' | head
```

## Going direct instead

```yaml
agent:
  model: "anthropic/claude-sonnet-4.5"
  modelApiKeyEnv: "ANTHROPIC_API_KEY"
```

```yaml
agent:
  model: "openai/gpt-5"
  modelApiKeyEnv: "OPENAI_API_KEY"
```

Then set `MODEL_API_KEY` to that provider's key. Nothing else changes.

## Operational notes for OpenRouter

Things that differ from a direct provider account, worth knowing before the
first incident:

- **It is prepaid.** No balance gives a `402`. Organisation budget limits give
  a `403` instead, checked before the key's own limit.
- **An in-flight spending budget caps concurrent estimated cost**, so several
  agents running at once can `402` while your balance looks fine. Relevant if
  you scale `agent.replicaCount` or enable KEDA.
- **Free (`:free`) slugs are rate-limited hard** — 20 requests/minute, and
  50/day below 10 purchased credits. Not suitable for an agent.
- **Upstream routing is nondeterministic by default.** The same slug may be
  served by different upstream providers with different context limits,
  quantisation and tool-call fidelity.
- **Prompt caching economics are likely worse** than talking to a vendor
  directly.
- **No subscription or OAuth path** — pay per token only.

`GET https://openrouter.ai/api/v1/key` reports remaining quota across all
three limit types, which is the thing to check when agents start failing.

## Verifying after a deploy

There is no `opencode models` command in v2. To confirm the credential
arrived:

```bash
# Did opencode see the variable? Look for type=environment.
kubectl -n mushroom exec deploy/mushroom-agent -- \
  sh -c 'opencode auth list'

# End to end, cheaply.
kubectl -n mushroom exec deploy/mushroom-agent -- \
  sh -c 'opencode run --standalone --model openrouter/anthropic/claude-haiku-4.5 "say ok"'
```

One precedence rule worth knowing: if anyone has ever run an interactive
`/connect` for the same provider on that server, **the saved account wins over
the environment variable**. In a container that should never happen, but it
explains an otherwise baffling "wrong credential" symptom.

## A note on reasoning variants

opencode supports a `#high` / `#low` suffix for reasoning effort
(`openrouter/anthropic/claude-sonnet-4.5#high`), but **the root `model` setting
does not retain the variant** — it is only honoured per run, session, agent or
command. Setting it in `agent.model` silently drops it.
