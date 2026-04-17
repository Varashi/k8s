{{- define "k8s-talos.installImage" -}}
{{ .Values.installer.image }}:{{ .Values.installer.version }}
{{- end }}

{{/*
  GPU worker installer image — hardcoded GPU schematic (i915 + intel-ucode +
  iscsi-tools + util-linux-tools + mei on top of standard vmtoolsd-guest-agent).
  Baked into worker-gpu class template so every gpu-worker* node gets the same
  schematic without per-node values overrides. Regenerate schematic at
  https://factory.talos.dev if extensions change. Version still comes from
  cluster-wide values.installer.version so upgrades are uniform.
*/}}
{{- define "k8s-talos.gpuInstallImage" -}}
factory.talos.dev/installer/d561b3847baff0099e71d536c1144eab0eeab32fada49913901d034a3d7d4503:{{ .Values.installer.version }}
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
