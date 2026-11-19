{{- define "alephat.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "alephat.postgresHost" -}}
{{- printf "alephat-postgresql.%s.svc.cluster.local" .Values.namespaces.postgresql -}}
{{- end -}}

{{- define "alephat.temporalHost" -}}
{{- printf "temporal.%s.svc.cluster.local" .Values.namespaces.temporal -}}
{{- end -}}

{{- define "alephat.temporalAddress" -}}
{{- printf "%s:7233" (include "alephat.temporalHost" .) -}}
{{- end -}}

{{- define "alephat.litellmBaseUrl" -}}
{{- printf "http://litellm.%s.svc.cluster.local:4000" .Values.namespaces.litellm -}}
{{- end -}}

{{- define "alephat.runtimeClassName" -}}
{{- if eq .Values.sandboxRuntime.type "kata" -}}
{{- .Values.sandboxRuntime.kata.runtimeClassName -}}
{{- else -}}
{{- .Values.sandboxRuntime.gvisor.runtimeClassName -}}
{{- end -}}
{{- end -}}

{{- define "alephat.enabledModelsCsv" -}}
{{- $aliases := list -}}
{{- range .Values.litellm.models -}}
{{- $aliases = append $aliases .alias -}}
{{- end -}}
{{- join "," $aliases -}}
{{- end -}}

{{- define "alephat.publicOrigin" -}}
{{- if and .Values.traefik.enabled .Values.ingress.enabled .Values.ingress.host -}}
{{- printf "https://%s" .Values.ingress.host -}}
{{- else -}}
{{- .Values.alephat.config.dashboardBaseUrl -}}
{{- end -}}
{{- end -}}

{{/*
Generate or reuse shared secrets when externalSecrets.enabled=false so cross-namespace
references receive identical credentials and survive `helm upgrade`.
*/}}
{{- define "alephat.generatedSecrets" -}}
{{- if not (hasKey . "_generatedSecrets") -}}
  {{- $existingPg := (lookup "v1" "Secret" .Values.namespaces.postgresql "alephat-postgres") -}}
  {{- $pgPass := .Values.secrets.postgresPassword -}}
  {{- if and (not $pgPass) $existingPg (hasKey $existingPg "data") (hasKey $existingPg.data "password") -}}
    {{- $pgPass = (index $existingPg.data "password" | b64dec) -}}
  {{- end -}}
  {{- if not $pgPass -}}
    {{- $pgPass = (randAlphaNum 32) -}}
  {{- end -}}

  {{- $existingLiteMaster := (lookup "v1" "Secret" .Values.namespaces.litellm "litellm-secrets") -}}
  {{- $liteMaster := .Values.secrets.litellmMasterKey -}}
  {{- if and (not $liteMaster) $existingLiteMaster (hasKey $existingLiteMaster "data") (hasKey $existingLiteMaster.data "masterkey") -}}
    {{- $liteMaster = (index $existingLiteMaster.data "masterkey" | b64dec) -}}
  {{- end -}}
  {{- if not $liteMaster -}}
    {{- $liteMaster = (printf "sk-%s" (randAlphaNum 32)) -}}
  {{- end -}}

  {{- $existingLitePg := (lookup "v1" "Secret" .Values.namespaces.litellm "litellm-postgres") -}}
  {{- $litePg := .Values.secrets.litellmPostgresPassword -}}
  {{- if and (not $litePg) $existingLitePg (hasKey $existingLitePg "data") (hasKey $existingLitePg.data "password") -}}
    {{- $litePg = (index $existingLitePg.data "password" | b64dec) -}}
  {{- end -}}
  {{- if not $litePg -}}
    {{- $litePg = (randAlphaNum 32) -}}
  {{- end -}}

  {{- $existingTls := (lookup "v1" "Secret" .Values.namespaces.temporal "temporal-tls") -}}
  {{- $caCrt := "" -}}
  {{- $srvCrt := "" -}}
  {{- $srvKey := "" -}}
  {{- $cliCrt := "" -}}
  {{- $cliKey := "" -}}
  {{- if and $existingTls (hasKey $existingTls "data") (hasKey $existingTls.data "ca.crt") -}}
    {{- $caCrt = (index $existingTls.data "ca.crt" | b64dec) -}}
    {{- $srvCrt = (index $existingTls.data "server.crt" | b64dec) -}}
    {{- $srvKey = (index $existingTls.data "server.key" | b64dec) -}}
    {{- $cliCrt = (index $existingTls.data "tls.crt" | b64dec) -}}
    {{- $cliKey = (index $existingTls.data "tls.key" | b64dec) -}}
  {{- else -}}
    {{- $ca := genCA "Alephat Temporal Root CA" 3650 -}}
    {{- $tNs := .Values.namespaces.temporal -}}
    {{- $sans := list "temporal" (printf "temporal.%s" $tNs) (printf "temporal.%s.svc" $tNs) (printf "temporal.%s.svc.cluster.local" $tNs) "localhost" -}}
    {{- if .Values.ingress.temporalGrpcHost -}}
      {{- $sans = append $sans .Values.ingress.temporalGrpcHost -}}
    {{- end -}}
    {{- $srv := genSignedCert (printf "temporal.%s.svc.cluster.local" $tNs) (list "127.0.0.1") $sans 3650 $ca -}}
    {{- $cli := genSignedCert "alephat-temporal-client" nil nil 3650 $ca -}}
    {{- $caCrt = $ca.Cert -}}
    {{- $srvCrt = $srv.Cert -}}
    {{- $srvKey = $srv.Key -}}
    {{- $cliCrt = $cli.Cert -}}
    {{- $cliKey = $cli.Key -}}
  {{- end -}}

  {{- $_ := set . "_generatedSecrets" (dict
      "postgresPassword" $pgPass
      "litellmMasterKey" $liteMaster
      "litellmPostgresPassword" $litePg
      "caCrt" $caCrt
      "serverCrt" $srvCrt
      "serverKey" $srvKey
      "clientCrt" $cliCrt
      "clientKey" $cliKey
  ) -}}
{{- end -}}
{{- end -}}
