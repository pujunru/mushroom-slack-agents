{{/*
Shared template helpers.
*/}}

{{- define "mushroom.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mushroom.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "mushroom.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mushroom.labels" -}}
helm.sh/chart: {{ include "mushroom.chart" . }}
{{ include "mushroom.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mushroom.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mushroom.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mushroom.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mushroom.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
The Secret that holds every credential, whichever provider produced it.
*/}}
{{- define "mushroom.secretName" -}}
{{- if eq .Values.secrets.provider "existing" }}
{{- required "secrets.existing.name is required when secrets.provider=existing" .Values.secrets.existing.name }}
{{- else }}
{{- printf "%s-secrets" (include "mushroom.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Image reference for a component, e.g. {{ include "mushroom.image" (dict "root" . "component" "gateway") }}
*/}}
{{- define "mushroom.image" -}}
{{- $root := .root -}}
{{- $tag := index $root.Values.image.tags .component -}}
{{/*
`toString` is load-bearing. An all-digit tag — a git SHA like 8458615, which
happens roughly one time in sixteen — is parsed from YAML as a number, and
printf "%s" on an int64 renders the literal text "%!s(int64=8458615)". That
becomes the image reference and the pod fails with InvalidImageName, which
reads like a registry problem rather than a templating one.
*/}}
{{- printf "%s/%s-%s:%s" $root.Values.image.registry $root.Values.image.repository .component (toString $tag) -}}
{{- end }}

{{/*
Postgres DSN. Built from the bundled chart's service when enabled, otherwise
taken from the secret so the password never appears in a manifest.
*/}}
{{- define "mushroom.databaseUrlFromValues" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "postgresql://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password (include "mushroom.fullname" .) .Values.postgresql.auth.database -}}
{{- else -}}
{{- printf "postgresql://%s@%s:%v/%s" .Values.externalDatabase.user .Values.externalDatabase.host .Values.externalDatabase.port .Values.externalDatabase.database -}}
{{- end -}}
{{- end }}

{{/*
Environment shared by the gateway, the workers and the migration job.
Credentials always arrive by secretKeyRef, never as literal values.
*/}}
{{- define "mushroom.commonEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "mushroom.secretName" . }}
      key: {{ .Values.secrets.keys.databaseUrl }}
- name: LOG_LEVEL
  value: {{ .Values.observability.logLevel | quote }}
- name: LOG_FORMAT
  value: {{ .Values.observability.logFormat | quote }}
- name: METRICS_PORT
  value: {{ .Values.observability.metricsPort | quote }}
- name: QUEUE_NAME
  value: "mushroom_jobs"
- name: DEAD_LETTER_QUEUE_NAME
  value: "mushroom_jobs_dlq"
{{- end }}

{{/*
Hook annotations for the bundled Postgres.

Weight -8 places it after the ServiceAccount and Secret (-10) and before the
migration Job (-5), so the Job finds a database to connect to.

`keep` protects the data from `helm uninstall`, and `before-hook-creation` is
deliberately absent: deleting and recreating the StatefulSet on every upgrade
would restart the database on each deploy.
*/}}
{{- define "mushroom.postgresHookAnnotations" -}}
"helm.sh/hook": pre-install,pre-upgrade
"helm.sh/hook-weight": "-8"
"helm.sh/resource-policy": keep
{{- end }}

{{/*
Flatten agent.skills into a JSON list of {key, path, content}.

A skill is either a string (the SKILL.md body) or a map of relative path to
file content, which lets a skill carry scripts and references beside its
SKILL.md. `key` is the ConfigMap key and `path` is where the file lands under
the custom skills directory.
*/}}
{{- define "mushroom.agentSkillFiles" -}}
{{- $files := list }}
{{- range $name, $skill := .Values.agent.skills }}
{{- if not (regexMatch "^[a-z0-9]+(-[a-z0-9]+)*$" $name) }}
{{- fail (printf "agent.skills: %q is not a valid skill name (lowercase letters, digits and single hyphens)" $name) }}
{{- end }}
{{- $tree := $skill }}
{{- if kindIs "string" $skill }}
{{- $tree = dict "SKILL.md" $skill }}
{{- end }}
{{- if not (hasKey $tree "SKILL.md") }}
{{- fail (printf "agent.skills.%s: a skill given as a map must include SKILL.md" $name) }}
{{- end }}
{{- range $path, $content := $tree }}
{{- if or (hasPrefix "/" $path) (contains ".." $path) }}
{{- fail (printf "agent.skills.%s: %q must be a relative path inside the skill" $name $path) }}
{{- end }}
{{- $key := printf "%s--%s" $name ($path | replace "/" "--") }}
{{- $files = append $files (dict "key" $key "path" (printf "%s/%s" $name $path) "content" $content) }}
{{- end }}
{{- end }}
{{- toJson $files }}
{{- end }}
