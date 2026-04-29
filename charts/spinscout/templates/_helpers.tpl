{{- define "spinscout.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "spinscout.fullname" -}}
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

{{- define "spinscout.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "spinscout.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "spinscout.selectorLabels" -}}
app.kubernetes.io/name: {{ include "spinscout.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "spinscout.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "spinscout.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "spinscout.apiSecretName" -}}
{{- if .Values.api.existingSecret -}}
{{- .Values.api.existingSecret -}}
{{- else -}}
{{- printf "%s-api-secrets" (include "spinscout.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "spinscout.apiConnectionsSecretName" -}}
{{- printf "%s-api-connections" (include "spinscout.fullname" .) -}}
{{- end -}}

{{- define "spinscout.postgresqlSecretName" -}}
{{- printf "%s-postgresql" (include "spinscout.fullname" .) -}}
{{- end -}}

{{- define "spinscout.postgresqlHost" -}}
{{- printf "%s-postgresql" (include "spinscout.fullname" .) -}}
{{- end -}}

{{- define "spinscout.redisHost" -}}
{{- printf "%s-redis" (include "spinscout.fullname" .) -}}
{{- end -}}

{{- define "spinscout.databaseUrl" -}}
{{- if .Values.externalDatabase.url -}}
{{- .Values.externalDatabase.url -}}
{{- else -}}
{{- printf "postgresql+psycopg://%s:%s@%s:%v/%s" .Values.postgresql.username .Values.postgresql.password (include "spinscout.postgresqlHost" .) .Values.postgresql.service.port .Values.postgresql.database -}}
{{- end -}}
{{- end -}}

{{- define "spinscout.redisUrl" -}}
{{- if .Values.externalRedis.url -}}
{{- .Values.externalRedis.url -}}
{{- else -}}
{{- printf "redis://%s:%v/%v" (include "spinscout.redisHost" .) .Values.redis.service.port .Values.redis.database -}}
{{- end -}}
{{- end -}}
