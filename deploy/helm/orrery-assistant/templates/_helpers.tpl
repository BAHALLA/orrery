{{/*
Common helpers for the orrery-assistant chart.
*/}}

{{- define "orrery-assistant.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "orrery-assistant.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "orrery-assistant.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "orrery-assistant.labels" -}}
helm.sh/chart: {{ include "orrery-assistant.chart" . }}
{{ include "orrery-assistant.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: orrery
{{- end -}}

{{- define "orrery-assistant.selectorLabels" -}}
app.kubernetes.io/name: {{ include "orrery-assistant.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "orrery-assistant.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "orrery-assistant.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "orrery-assistant.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "orrery-assistant.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "orrery-assistant.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{/*
Pub/Sub worker — rendered as a separate Deployment. The `app.kubernetes.io/name`
label carries a distinct value so the main Deployment's selector never matches
worker pods (Deployment selectors are immutable, so overlap would be fatal).
*/}}

{{- define "orrery-assistant.pubsubWorker.name" -}}
{{- printf "%s-pubsub-worker" (include "orrery-assistant.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "orrery-assistant.pubsubWorker.fullname" -}}
{{- printf "%s-pubsub-worker" (include "orrery-assistant.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "orrery-assistant.pubsubWorker.selectorLabels" -}}
app.kubernetes.io/name: {{ include "orrery-assistant.pubsubWorker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "orrery-assistant.pubsubWorker.labels" -}}
helm.sh/chart: {{ include "orrery-assistant.chart" . }}
{{ include "orrery-assistant.pubsubWorker.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: orrery
app.kubernetes.io/component: pubsub-worker
{{- end -}}

{{- define "orrery-assistant.pubsubWorker.serviceAccountName" -}}
{{- if .Values.pubsubWorker.serviceAccount.create -}}
{{- default (include "orrery-assistant.pubsubWorker.fullname" .) .Values.pubsubWorker.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.pubsubWorker.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
True when the HTTP deployment can run more than one pod at a time.
*/}}
{{- define "orrery-assistant.multiReplica" -}}
{{- if or .Values.autoscaling.enabled (gt (int .Values.replicaCount) 1) -}}true{{- end -}}
{{- end }}

{{/*
Where pending approvals live: an explicit config.ORRERY_CONFIRMATION_BACKEND
wins (backwards compatible), otherwise persistence.backend.
*/}}
{{- define "orrery-assistant.confirmationBackend" -}}
{{- index .Values.config "ORRERY_CONFIRMATION_BACKEND" | default .Values.persistence.backend -}}
{{- end }}

{{/*
MCP servers file: rendered from .Values.mcp.servers, or an existing ConfigMap
holding an `mcp.yaml` key. Fails the render on an enabled-but-empty config —
starting without the tools (and guards) the operator meant to declare is worse
than not starting.
*/}}
{{- define "orrery-assistant.mcpInlineConfig" -}}
{{- if and .Values.mcp.enabled (not .Values.mcp.existingConfigMap) -}}
{{- if not .Values.mcp.servers -}}
{{- fail "mcp.enabled is true but mcp.servers is empty and mcp.existingConfigMap is unset. Declare at least one server, or point existingConfigMap at a ConfigMap with an mcp.yaml key." -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{- define "orrery-assistant.mcpConfigMapName" -}}
{{- .Values.mcp.existingConfigMap | default (printf "%s-mcp" (include "orrery-assistant.fullname" .)) -}}
{{- end -}}


{{/*
The agent's RBAC rules. "cluster" adds the cluster-scoped reads a Role cannot
grant (nodes, namespaces, CRDs, node metrics).
*/}}
{{- define "orrery-assistant.rbacRules" -}}
{{- $rbac := .Values.rbac -}}
- apiGroups: [""]
  resources: ["pods", "pods/log", "events", "services", "endpoints", "configmaps"]
  verbs: ["get", "list"]
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets", "replicasets", "daemonsets"]
  verbs: ["get", "list"]
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list"]
- apiGroups: ["metrics.k8s.io"]
  resources: ["pods"]
  verbs: ["get", "list"]
{{- if .cluster }}
- apiGroups: [""]
  resources: ["nodes", "namespaces"]
  verbs: ["get", "list"]
- apiGroups: ["metrics.k8s.io"]
  resources: ["nodes"]
  verbs: ["get", "list"]
- apiGroups: ["apiextensions.k8s.io"]
  resources: ["customresourcedefinitions"]
  verbs: ["get", "list"]
{{- end }}
{{- if $rbac.operators.strimzi }}
- apiGroups: ["kafka.strimzi.io"]
  resources:
    - kafkas
    - kafkanodepools
    - kafkatopics
    - kafkausers
    - kafkaconnects
    - kafkaconnectors
    - kafkamirrormaker2s
    - kafkarebalances
  verbs: ["get", "list"]
{{- end }}
{{- if $rbac.operators.eck }}
- apiGroups: ["elasticsearch.k8s.elastic.co"]
  resources: ["elasticsearches"]
  verbs: ["get", "list"]
- apiGroups: ["kibana.k8s.elastic.co"]
  resources: ["kibanas"]
  verbs: ["get", "list"]
{{- end }}
{{- if eq $rbac.profile "operate" }}
# The writes the mutating tools make: scale, restart, rollback and patch.
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets"]
  verbs: ["patch"]
- apiGroups: ["apps"]
  resources: ["deployments/scale"]
  verbs: ["patch"]
{{- if $rbac.operators.strimzi }}
# Approving a rebalance and restarting a connector are annotations.
- apiGroups: ["kafka.strimzi.io"]
  resources: ["kafkarebalances", "kafkaconnectors"]
  verbs: ["patch"]
{{- end }}
{{- end }}
{{- with $rbac.extraRules }}
{{ toYaml . }}
{{- end }}
{{- end -}}
