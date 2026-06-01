{{/*
Standard Helm helpers — name, fullname, labels, selectorLabels, serviceAccountName.
Kept minimal so a `helm template` diff is easy to scan.
*/}}

{{/* The chart's short name; respects nameOverride. */}}
{{- define "darwin-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The chart's release-fullname; respects fullnameOverride. */}}
{{- define "darwin-platform.fullname" -}}
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

{{/* Common labels — applied to every resource the chart owns. */}}
{{- define "darwin-platform.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "darwin-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: supervisor
app.kubernetes.io/part-of: darwin-platform
{{- end -}}

{{/* Selector labels — the subset that's stable across upgrades. */}}
{{- define "darwin-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "darwin-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* ServiceAccount name resolution. */}}
{{- define "darwin-platform.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "darwin-platform.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Image tag resolution — empty values.image.tag falls back to appVersion. */}}
{{- define "darwin-platform.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}
