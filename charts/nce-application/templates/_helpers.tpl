{{- define "nce.name" -}}
{{- .Release.Name | trunc 45 | trimSuffix "-" -}}
{{- end -}}
{{- define "nce.labels" -}}
app.kubernetes.io/name: {{ include "nce.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: Helm
nce.core/source-commit: {{ .Values.releaseIdentity.sourceCommit | quote }}
{{- end -}}
