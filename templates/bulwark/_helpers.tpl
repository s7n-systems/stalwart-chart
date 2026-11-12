{{/* webmail.ingress.host as configured, or webmail.<domain> derived
     from the top-level domain when left empty. */}}
{{- define "stalwart.webmailIngressHost" -}}
{{- if .Values.webmail.ingress.host -}}
{{- .Values.webmail.ingress.host -}}
{{- else if .Values.domain -}}
{{- printf "webmail.%s" .Values.domain -}}
{{- else -}}
{{- fail "webmail.ingress.enabled requires webmail.ingress.host or a top-level domain" -}}
{{- end -}}
{{- end -}}

{{/* webmail.gatewayAPI.httpRoute.hostnames as configured, or a single
     webmail.<domain> default derived from the top-level domain. */}}
{{- define "stalwart.webmailGatewayHostnames" -}}
{{- if .Values.webmail.gatewayAPI.httpRoute.hostnames -}}
{{- .Values.webmail.gatewayAPI.httpRoute.hostnames | toYaml -}}
{{- else if .Values.domain -}}
{{- list (printf "webmail.%s" .Values.domain) | toYaml -}}
{{- else -}}
{{- fail "webmail.gatewayAPI.httpRoute.enabled requires hostnames or a top-level domain" -}}
{{- end -}}
{{- end -}}

{{/* Bulwark webmail: a separate component, so it gets its own name,
     selector (name+instance+component=webmail, to avoid matching the
     main StatefulSet's pods or the internal management Service), and
     image reference. */}}
{{- define "stalwart.webmailFullname" -}}
{{- printf "%s-webmail" (include "stalwart.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "stalwart.webmailAnnotations" -}}
{{- merge (.specific | default dict) .context.Values.webmail.additionalAnnotations | toYaml -}}
{{- end -}}

{{- define "stalwart.webmailLabels" -}}
{{- include "common.labels.standard" (dict "customLabels" (merge (dict "app.kubernetes.io/component" "webmail") (omit .Values.webmail.additionalLabels "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component")) "context" .) -}}
{{- end -}}

{{- define "stalwart.webmailSelectorLabels" -}}
{{ include "common.labels.matchLabels" . }}
app.kubernetes.io/component: webmail
{{- end -}}

{{- define "stalwart.webmailImage" -}}
{{- include "common.images.image" (dict "imageRoot" .Values.webmail.image "global" .Values.global "chart" .Chart) -}}
{{- end -}}

{{- define "stalwart.webmailServiceAccountName" -}}
{{- if .Values.webmail.serviceAccount.create -}}
{{- default (include "stalwart.webmailFullname" .) .Values.webmail.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.webmail.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Falls back to the internal management Service, Stalwart's plain HTTP
     listener, which always exists (a new server has no listener on port 80). */}}
{{- define "stalwart.webmailJmapUrl" -}}
{{- if .Values.webmail.jmapServerUrl -}}
{{- .Values.webmail.jmapServerUrl -}}
{{- else -}}
{{- printf "http://%s-management:%v" (include "stalwart.fullname" .) .Values.management.servicePort -}}
{{- end -}}
{{- end -}}

{{/* True when a chart-managed credentials Secret is needed for the
     webmail component's plain-value fallbacks. */}}
{{- define "stalwart.webmailNeedsManagedSecret" -}}
{{- if and .Values.webmail.enabled (or
  (and (not .Values.webmail.sessionSecret.existingSecret) .Values.webmail.sessionSecret.value)
  (and (not .Values.webmail.adminPassword.existingSecret) .Values.webmail.adminPassword.value)
  (and .Values.webmail.oauth.enabled (not .Values.webmail.oauth.existingSecret) .Values.webmail.oauth.clientSecret)
  (and (not .Values.webmail.sharedStore.existingSecret) .Values.webmail.sharedStore.url)
) -}}
true
{{- end -}}
{{- end -}}

{{- define "stalwart.webmailEffectiveMaxReplicas" -}}
{{- if .Values.webmail.autoscaling.enabled -}}
{{- .Values.webmail.autoscaling.maxReplicas -}}
{{- else -}}
{{- .Values.webmail.replicaCount -}}
{{- end -}}
{{- end -}}

{{/* Bulwark-specific checks, called from stalwart.validateValues. */}}
{{- define "stalwart.validateWebmail" -}}
{{- if and (gt (len .Values.webmail.service.ipFamilies) 1) (not (has .Values.webmail.service.ipFamilyPolicy (list "PreferDualStack" "RequireDualStack"))) -}}
{{- fail "webmail.service.ipFamilies with two entries requires webmail.service.ipFamilyPolicy PreferDualStack or RequireDualStack" -}}
{{- end -}}
{{- if and .Values.webmail.ingress.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.ingress.enabled requires webmail.enabled" -}}
{{- end -}}
{{- if and .Values.webmail.gatewayAPI.httpRoute.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.gatewayAPI.httpRoute.enabled requires webmail.enabled" -}}
{{- end -}}
{{- if and .Values.webmail.gatewayAPI.httpRoute.enabled (not .Values.webmail.gatewayAPI.httpRoute.parentRefs) -}}
{{- fail "webmail.gatewayAPI.httpRoute.parentRefs must reference an existing Gateway" -}}
{{- end -}}
{{- if and .Values.webmail.oauth.enabled (not .Values.webmail.oauth.clientId) -}}
{{- fail "webmail.oauth.clientId is required when webmail.oauth.enabled=true" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (not .Values.webmail.enabled) -}}
{{- fail "webmail.autoscaling.enabled requires webmail.enabled" -}}
{{- end -}}
{{/* A shared volume cannot synchronize process-local limits, leases or session revocations. */}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) (not (or .Values.webmail.sharedStore.existingSecret .Values.webmail.sharedStore.url)) -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires webmail.sharedStore: without it each replica keeps its own sign-in limits and admin sessions, which a shared volume does not synchronize" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) .Values.webmail.persistence.enabled (eq .Values.webmail.persistence.accessMode "ReadWriteOnce") -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 cannot share a ReadWriteOnce volume: with webmail.sharedStore the replicas need none, so set webmail.persistence.enabled=false (after the one-replica import, see the README)" -}}
{{- end -}}
{{- if and .Values.webmail.settingsSync.enabled (not .Values.webmail.persistence.enabled) (not (or .Values.webmail.sharedStore.existingSecret .Values.webmail.sharedStore.url)) -}}
{{- fail "webmail.settingsSync.enabled requires webmail.persistence.enabled or webmail.sharedStore" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) (not .Values.webmail.adminPassword.existingSecret) (not .Values.webmail.adminPassword.value) -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires webmail.adminPassword.existingSecret or .value (use the existing password when migrating)" -}}
{{- end -}}
{{- if and (gt (int (include "stalwart.webmailEffectiveMaxReplicas" .)) 1) (not .Values.webmail.sessionSecret.existingSecret) -}}
{{- if not .Values.webmail.sessionSecret.value -}}
{{- fail "webmail.replicaCount/autoscaling.maxReplicas > 1 requires webmail.sessionSecret.existingSecret or .value. Preserve the existing secret when migrating; see the README" -}}
{{- else if lt (len .Values.webmail.sessionSecret.value) 32 -}}
{{- fail "webmail.sessionSecret.value must be at least 32 characters for multi-replica admin sessions" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.webmail.oauth.autoSso (not .Values.webmail.oauth.only) -}}
{{- fail "webmail.oauth.autoSso requires webmail.oauth.only" -}}
{{- end -}}
{{- if and .Values.webmail.oauth.passwordFallback (not .Values.webmail.oauth.only) -}}
{{- fail "webmail.oauth.passwordFallback requires webmail.oauth.only" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (gt (int .Values.webmail.autoscaling.minReplicas) (int .Values.webmail.autoscaling.maxReplicas)) -}}
{{- fail "webmail.autoscaling.minReplicas must not be greater than webmail.autoscaling.maxReplicas" -}}
{{- end -}}
{{- if and .Values.webmail.pdb.create (eq (toString .Values.webmail.pdb.minAvailable) "") (eq (toString .Values.webmail.pdb.maxUnavailable) "") -}}
{{- fail "webmail.pdb.create requires webmail.pdb.maxUnavailable or webmail.pdb.minAvailable" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled (not .Values.webmail.autoscaling.metrics) (not .Values.webmail.autoscaling.targetCPUUtilizationPercentage) (not .Values.webmail.autoscaling.targetMemoryUtilizationPercentage) -}}
{{- fail "webmail.autoscaling.enabled requires targetCPUUtilizationPercentage, targetMemoryUtilizationPercentage, or metrics" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled .Values.webmail.autoscaling.targetCPUUtilizationPercentage (not (dig "requests" "cpu" false (.Values.webmail.resources | default dict))) -}}
{{- fail "webmail.autoscaling.targetCPUUtilizationPercentage requires webmail.resources.requests.cpu" -}}
{{- end -}}
{{- if and .Values.webmail.autoscaling.enabled .Values.webmail.autoscaling.targetMemoryUtilizationPercentage (not (dig "requests" "memory" false (.Values.webmail.resources | default dict))) -}}
{{- fail "webmail.autoscaling.targetMemoryUtilizationPercentage requires webmail.resources.requests.memory" -}}
{{- end -}}
{{- end -}}

{{/* Environment generated from webmail values; merged with extraEnv/extraEnvVars by stalwart.mergeEnv. */}}
{{- define "stalwart.webmailGeneratedEnv" }}
- name: PORT
  value: {{ .Values.webmail.service.port | quote }}
- name: JMAP_SERVER_URL
  value: {{ include "stalwart.webmailJmapUrl" . | quote }}
{{- if .Values.webmail.appName }}
- name: APP_NAME
  value: {{ .Values.webmail.appName | quote }}
{{- end }}
{{- include "stalwart.webmailBrandingEnv" . }}
- name: STALWART_FEATURES
  value: {{ .Values.webmail.stalwartFeatures | quote }}
- name: SETTINGS_SYNC_ENABLED
  value: {{ .Values.webmail.settingsSync.enabled | quote }}
- name: TRUSTED_PROXY_DEPTH
  value: {{ .Values.webmail.trustedProxyDepth | quote }}
- name: LOG_FORMAT
  value: {{ .Values.webmail.logFormat | quote }}
- name: LOG_LEVEL
  value: {{ .Values.webmail.logLevel | quote }}
{{- if .Values.webmail.adminConfigReadonly }}
- name: ADMIN_CONFIG_READONLY
  value: "true"
{{- end }}
{{- if not .Values.webmail.telemetry.enabled }}
- name: BULWARK_TELEMETRY
  value: "off"
{{- end }}
{{- if (or .Values.webmail.sharedStore.existingSecret .Values.webmail.sharedStore.url) }}
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      {{- if .Values.webmail.sharedStore.existingSecret }}
      name: {{ .Values.webmail.sharedStore.existingSecret | quote }}
      key: {{ .Values.webmail.sharedStore.secretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.webmailFullname" . }}-env
      key: redisUrl
      {{- end }}
{{- with .Values.webmail.sharedStore.keyPrefix }}
- name: REDIS_KEY_PREFIX
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- if or .Values.webmail.sessionSecret.existingSecret .Values.webmail.sessionSecret.value }}
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef:
      {{- if .Values.webmail.sessionSecret.existingSecret }}
      name: {{ .Values.webmail.sessionSecret.existingSecret | quote }}
      key: {{ .Values.webmail.sessionSecret.secretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.webmailFullname" . }}-env
      key: sessionSecret
      {{- end }}
{{- end }}
{{- if or .Values.webmail.adminPassword.existingSecret .Values.webmail.adminPassword.value }}
- name: ADMIN_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if .Values.webmail.adminPassword.existingSecret }}
      name: {{ .Values.webmail.adminPassword.existingSecret | quote }}
      key: {{ .Values.webmail.adminPassword.secretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.webmailFullname" . }}-env
      key: adminPassword
      {{- end }}
{{- end }}
{{- if .Values.webmail.oauth.enabled }}
- name: OAUTH_ENABLED
  value: "true"
- name: OAUTH_ONLY
  value: {{ .Values.webmail.oauth.only | quote }}
- name: AUTO_SSO_ENABLED
  value: {{ .Values.webmail.oauth.autoSso | quote }}
{{- if .Values.webmail.oauth.passwordFallback }}
- name: OAUTH_PASSWORD_FALLBACK_ENABLED
  value: "true"
{{- end }}
- name: OAUTH_CLIENT_ID
  value: {{ .Values.webmail.oauth.clientId | quote }}
{{- if or .Values.webmail.oauth.existingSecret .Values.webmail.oauth.clientSecret }}
- name: OAUTH_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      {{- if .Values.webmail.oauth.existingSecret }}
      name: {{ .Values.webmail.oauth.existingSecret | quote }}
      key: {{ .Values.webmail.oauth.clientSecretKey | quote }}
      {{- else }}
      name: {{ include "stalwart.webmailFullname" . }}-env
      key: oauthClientSecret
      {{- end }}
{{- end }}
{{- $issuerUrl := .Values.webmail.oauth.issuerUrl | default (and .Values.oidc.enabled .Values.oidc.issuerUrl) }}
{{- if $issuerUrl }}
- name: OAUTH_ISSUER_URL
  value: {{ $issuerUrl | quote }}
{{- end }}
{{- if .Values.webmail.oauth.scopes }}
- name: OAUTH_SCOPES
  value: {{ .Values.webmail.oauth.scopes | quote }}
{{- end }}
{{- if .Values.webmail.oauth.extraScopes }}
- name: OAUTH_EXTRA_SCOPES
  value: {{ .Values.webmail.oauth.extraScopes | quote }}
{{- end }}
- name: OAUTH_END_SESSION
  value: {{ .Values.webmail.oauth.endSession | quote }}
{{- with .Values.webmail.oauth.endSessionUrl }}
- name: OAUTH_END_SESSION_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.webmail.oauth.postLogoutRedirectUri }}
- name: OAUTH_POST_LOGOUT_REDIRECT_URI
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/* The ConfigMap with the branding files, or "" when there are none. */}}
{{- define "stalwart.webmailBrandingConfigMap" -}}
{{- $b := .Values.webmail.branding -}}
{{- if $b.existingConfigMap -}}
{{- $b.existingConfigMap -}}
{{- else if or $b.files $b.binaryFiles -}}
{{- printf "%s-branding" (include "stalwart.webmailFullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Hostnames the palette applies to: the webmail's own, as the HTTPRoute or Ingress serves it. */}}
{{- define "stalwart.webmailPaletteHosts" -}}
{{- $route := .Values.webmail.gatewayAPI.httpRoute -}}
{{- if and $route.enabled $route.hostnames -}}
{{- $route.hostnames | toJson -}}
{{- else if or .Values.webmail.ingress.host .Values.domain -}}
{{- list (include "stalwart.webmailIngressHost" .) | toJson -}}
{{- else -}}
{{- fail "webmail.branding.palette requires the webmail hostname: webmail.ingress.host, webmail.gatewayAPI.httpRoute.hostnames or a top-level domain; or set the palette per host in webmail.branding.domains" -}}
{{- end -}}
{{- end -}}

{{/* DOMAIN_BRANDING: the domains entries, then a palette entry for each webmail hostname
     without an exact entry. Bulwark prefers an exact host over a wildcard, so the generated
     entry carries the settings of the most specific matching "*." entry, whose own palette
     colours win over branding.palette. */}}
{{- define "stalwart.webmailDomainBranding" -}}
{{- $b := .Values.webmail.branding -}}
{{- $entries := $b.domains | default list -}}
{{- $palette := dict -}}
{{- with $b.palette.light }}{{ $_ := set $palette "light" . }}{{ end -}}
{{- with $b.palette.dark }}{{ $_ := set $palette "dark" . }}{{ end -}}
{{- if $palette -}}
{{- $generated := list -}}
{{- range $host := include "stalwart.webmailPaletteHosts" . | fromJsonArray -}}
{{- $exact := false -}}
{{- $wildcard := dict -}}
{{- $suffixLength := 0 -}}
{{- range $entries -}}
{{- $pattern := lower .host -}}
{{- if eq $pattern (lower $host) -}}
{{- $exact = true -}}
{{- else if hasPrefix "*." $pattern -}}
{{- $suffix := trimPrefix "*" $pattern -}}
{{- if and (hasSuffix $suffix (lower $host)) (gt (len $suffix) $suffixLength) -}}
{{- $wildcard = . -}}
{{- $suffixLength = len $suffix -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $exact -}}
{{- $entry := omit (deepCopy $wildcard) "host" "palette" -}}
{{- $_ := set $entry "host" $host -}}
{{- $_ := set $entry "palette" (mergeOverwrite (deepCopy $palette) (deepCopy ($wildcard.palette | default dict))) -}}
{{- $generated = append $generated $entry -}}
{{- end -}}
{{- end -}}
{{- $entries = concat $entries $generated -}}
{{- end -}}
{{- if $entries }}{{ $entries | toJson }}{{ end -}}
{{- end -}}

{{/* Branding environment from webmail.branding; empty values keep Bulwark's defaults. */}}
{{- define "stalwart.webmailBrandingEnv" -}}
{{- $b := .Values.webmail.branding -}}
{{- $l := $b.login -}}
{{- $vars := list
  (list "APP_SHORT_NAME" $b.shortName)
  (list "FAVICON_URL" $b.favicon)
  (list "PWA_ICON_URL" $b.pwaIcon)
  (list "PWA_THEME_COLOR" $b.themeColor)
  (list "PWA_BACKGROUND_COLOR" $b.backgroundColor)
  (list "APP_LOGO_LIGHT_URL" $b.appLogo.light)
  (list "APP_LOGO_DARK_URL" $b.appLogo.dark)
  (list "LOGIN_LOGO_LIGHT_URL" $l.logo.light)
  (list "LOGIN_LOGO_DARK_URL" $l.logo.dark)
  (list "LOGIN_LOGO_MAX_HEIGHT" $l.logo.maxHeight)
  (list "LOGIN_LOGO_MAX_WIDTH" $l.logo.maxWidth)
  (list "LOGIN_LAYOUT" $l.layout)
  (list "LOGIN_BACKGROUND_LIGHT_URL" $l.background.light)
  (list "LOGIN_BACKGROUND_DARK_URL" $l.background.dark)
  (list "LOGIN_BACKGROUND_DIM" $l.background.dim)
  (list "LOGIN_BACKGROUND_BLUR" $l.background.blur)
  (list "LOGIN_COMPANY_NAME" $l.companyName)
  (list "LOGIN_WEBSITE_URL" $l.websiteUrl)
  (list "LOGIN_IMPRINT_URL" $l.imprintUrl)
  (list "LOGIN_PRIVACY_POLICY_URL" $l.privacyPolicyUrl)
  (list "DOMAIN_BRANDING" (include "stalwart.webmailDomainBranding" .))
-}}
{{- range $vars }}
{{- if ne (toString (index . 1)) "" }}
- name: {{ index . 0 }}
  value: {{ index . 1 | toString | quote }}
{{- end }}
{{- end }}
{{- end -}}
