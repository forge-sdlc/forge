{{- define "forge.name" -}}forge{{- end }}
{{- define "forge.fullname" -}}{{ .Release.Name }}{{- end }}
{{- define "forge.labels" -}}
app.kubernetes.io/name: {{ include "forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
{{- define "forge.image" -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}
{{- define "forge.sandboxImage" -}}
{{ printf "%s:%s" .Values.sandboxImage.repository .Values.sandboxImage.tag }}
{{- end }}
