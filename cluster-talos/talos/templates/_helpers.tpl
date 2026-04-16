{{- define "k8s-talos.installImage" -}}
{{ .Values.installer.image }}:{{ .Values.installer.version }}
{{- end }}

{{/*
  Empty pod/service subnets. Required to override talm --full defaults
  (10.244.0.0/16, 10.96.0.0/12) which break Cilium native routing (host-scope
  IPs overlap → overlap diagnostic → static IPs fail). Include under
  cluster.network in every node template.
*/}}
{{- define "k8s-talos.emptyClusterSubnets" -}}
podSubnets: []
serviceSubnets: []
{{- end }}
