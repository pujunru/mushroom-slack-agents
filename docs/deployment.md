# Deployment

## Environment-specific configuration belongs elsewhere

This repo is deliberately environment-agnostic: it knows nothing about any
particular cluster, hostname or credential, which is what lets it be published
as-is. Per-cluster values, bootstrap automation, deploy scripts and live
validation belong in a separate private repo alongside it.

The reference layout is a sibling directory:

```
~/development/mushroom/        this repo: code, chart, Dockerfiles
~/development/mushroom-env/    private: cluster values, deploy + validation
```

That repo holds the Helm values overlay per cluster, the scripts that build and
deploy, and an end-to-end suite that runs against a live deployment. Nothing
below assumes it exists — the chart installs on its own — but the split is why
no hostname or secret appears anywhere in this repo.

## Prerequisites

- A Kubernetes cluster and Helm 3.
- Traefik, if you want the `IngressRoute` path. Otherwise set
  `ingress.enabled=true` for a plain Ingress with any controller.
- A public DNS name with TLS: Slack will not post to plain HTTP.
- Postgres with pgmq — see [postgres.md](postgres.md).

Optional: External Secrets Operator and Vault, KEDA for queue-depth scaling,
Prometheus Operator for the ServiceMonitor.

## Build and push

```bash
export REG=ghcr.io/your-org/mushroom VERSION=0.1.0

docker build -f images/gateway.Dockerfile -t $REG-gateway:$VERSION .
docker build -f images/agent.Dockerfile   -t $REG-agent:$VERSION .
docker build -f images/migrate.Dockerfile -t $REG-migrate:$VERSION .

docker push $REG-gateway:$VERSION
docker push $REG-agent:$VERSION
docker push $REG-migrate:$VERSION
```

The agent image pins `@opencode/cli@2.1.1`. Move it deliberately:

```bash
docker build -f images/agent.Dockerfile --build-arg OPENCODE_VERSION=2.1.1 ...
```

Note that `ghcr.io/anomalyco/opencode:latest` still serves the v1 line, which
is why the image installs a pinned npm package instead of using it as a base.

## Install

Validate first — the chart was authored without `helm` available, so lint it:

```bash
helm dependency update deploy/helm/mushroom
helm lint deploy/helm/mushroom
helm template mushroom deploy/helm/mushroom \
  --set secrets.plain.slackSigningSecret=x --set secrets.plain.slackBotToken=y \
  | kubectl apply --dry-run=client -f -
```

Then:

```bash
helm install mushroom deploy/helm/mushroom \
  --namespace mushroom --create-namespace \
  --set image.registry=ghcr.io \
  --set image.repository=your-org/mushroom \
  --set traefik.ingressRoute.host=mushroom.example.com \
  --set secrets.provider=plain \
  --set secrets.plain.slackSigningSecret="$SLACK_SIGNING_SECRET" \
  --set secrets.plain.slackBotToken="$SLACK_BOT_TOKEN" \
  --set secrets.plain.modelApiKey="$MODEL_API_KEY"
```

Migrations run as a pre-install hook, so the schema exists before any pod
starts. The runner advisory-locks the set, so concurrent attempts are safe.

## Secrets via Vault

The `plain` provider templates credentials into a Secret from your values,
which means they pass through your shell history and any CI log. Use it only
to evaluate.

```bash
export VAULT_ADDR=https://vault.example.com VAULT_TOKEN=<admin>
SLACK_SIGNING_SECRET=... SLACK_BOT_TOKEN=... MODEL_API_KEY=... \
  K8S_NAMESPACE=mushroom K8S_SERVICE_ACCOUNT=mushroom \
  ./scripts/vault-bootstrap.sh
```

That creates the KV v2 secret, a read-only policy, and a Kubernetes auth role
bound to the chart's ServiceAccount. Then:

```bash
helm upgrade --install mushroom deploy/helm/mushroom \
  --namespace mushroom \
  --set secrets.provider=externalSecrets \
  --set secrets.externalSecrets.vault.server=$VAULT_ADDR \
  --set secrets.externalSecrets.vault.auth.kubernetes.role=mushroom
```

How it fits together: ESO authenticates to Vault with the ServiceAccount
token, reads the KV path, and maintains a Kubernetes Secret. Pods read
environment variables from that Secret and never contact Vault, so the cluster
stores no long-lived Vault credential.

Two version details that break older examples:

- Manifests use `external-secrets.io/v1`. `v1beta1` stopped being served in
  ESO v0.17.0.
- Vault 2.x requires an `audience` on the Kubernetes role; the chart sets
  `vault` by default.

Install the operators with:

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace

helm repo add hashicorp https://helm.releases.hashicorp.com
helm install vault hashicorp/vault -n vault --create-namespace \
  --set "server.dev.enabled=true"     # dev mode: evaluation only
```

## Scaling

The gateway scales on CPU, which is a reasonable proxy for request volume:

```yaml
gateway:
  autoscaling: { enabled: true, minReplicas: 2, maxReplicas: 10 }
```

Workers should scale on queue depth instead, since an agent waiting on a model
looks idle:

```yaml
keda:
  enabled: true
  minReplicaCount: 0      # scale to zero when the queue is empty
  maxReplicaCount: 20
  queueDepthTarget: "5"   # visible messages per worker
```

Two settings interact and must stay consistent:

- `agent.visibilityTimeoutSeconds` **must exceed** `agent.jobTimeoutSeconds`,
  or a slow job is redelivered while it is still running. The worker also
  heartbeats its lease, but the timeout is the backstop.
- `agent.terminationGracePeriodSeconds` must exceed `jobTimeoutSeconds`, or
  Kubernetes kills an in-flight job during a rollout.

## Observability

```yaml
observability:
  serviceMonitor: { enabled: true }
```

Metrics worth alerting on:

| Metric | Alert when |
|---|---|
| `mushroom_queue_depth` | Sustained growth — workers cannot keep up. |
| `mushroom_agent_jobs_dead_lettered_total` | Any increase. |
| `mushroom_slack_events_rejected_total{reason="bad_signature"}` | A spike means a misconfigured secret or someone probing. |
| `mushroom_ingest_duration_seconds` | p99 approaching 3s — the Slack ack budget. |
| `mushroom_agent_jobs_completed_total{status="failed"}` | Rising failure ratio. |

## Upgrades

```bash
helm upgrade mushroom deploy/helm/mushroom --set image.tags.gateway=0.2.0
```

The gateway rolls with `maxUnavailable: 0`, so the Slack endpoint never drops
below the configured replica count. Workers roll with `maxUnavailable: 1` and
`maxSurge: 0` — they are interchangeable, an unacked message is simply
redelivered, and this avoids doubling agent cost mid-deploy.

## Hardening checklist

- [ ] `secrets.provider=externalSecrets`, not `plain`.
- [ ] `networkPolicy.enabled=true` (needs a CNI that enforces it).
- [ ] An external, backed-up Postgres; `postgresql.enabled=false`.
- [ ] Resource limits reviewed — the defaults are a starting point.
- [ ] `traefik.ingressRoute.middlewares.rateLimit` tuned to your event volume.
- [ ] Only `/slack/events` publicly routed; confirm `/stats` and `/events`
      are not reachable from outside.
- [ ] Agent credentials scoped to what the agents actually need. Agents run
      with `permissions: autoaccept`, so they have whatever access you mount.
