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
{{- define "forge.podSecurityContext" -}}
{{- $context := deepCopy .Values.podSecurityContext -}}
{{- if .Values.runtimeSecurity.runAsUser -}}
{{- $_ := set $context "runAsUser" (.Values.runtimeSecurity.runAsUser | int) -}}
{{- end -}}
{{- if .Values.runtimeSecurity.fsGroup -}}
{{- $_ := set $context "fsGroup" (.Values.runtimeSecurity.fsGroup | int) -}}
{{- $_ := set $context "fsGroupChangePolicy" "OnRootMismatch" -}}
{{- end -}}
{{- toYaml $context -}}
{{- end }}
