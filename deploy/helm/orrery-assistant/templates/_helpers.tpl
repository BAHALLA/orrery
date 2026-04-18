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
