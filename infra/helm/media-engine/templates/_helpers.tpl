{{/*
Common labels + name helpers.
*/}}
{{- define "media-engine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "media-engine.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "media-engine.labels" -}}
app.kubernetes.io/name: {{ include "media-engine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "media-engine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "media-engine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
