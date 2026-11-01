#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
: "${AWS_PROFILE:?Set AWS_PROFILE in .env}"
: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID in .env}"
: "${AWS_SSO_SESSION:?Set AWS_SSO_SESSION in .env}"
: "${AWS_SSO_REGION:?Set AWS_SSO_REGION in .env}"
: "${AWS_SSO_START_URL:?Set AWS_SSO_START_URL in .env}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_CONFIG_FILE="$PWD/aws-config"
cat > "$AWS_CONFIG_FILE" <<EOF
[profile $AWS_PROFILE]
sso_session = $AWS_SSO_SESSION
sso_account_id = $AWS_ACCOUNT_ID
region = $AWS_REGION
output = json
sso_role_name = ${AWS_ROLE_NAME:-}

[sso-session $AWS_SSO_SESSION]
sso_start_url = $AWS_SSO_START_URL
sso_region = $AWS_SSO_REGION
sso_registration_scopes = sso:account:access
EOF
# Environment credentials take precedence over profiles in boto3.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN

expected_account="$AWS_ACCOUNT_ID"
configured_account="$(aws configure get sso_account_id --profile "$AWS_PROFILE" || true)"
if [[ "$configured_account" != "$expected_account" ]]; then
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt

if ! aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1; then
  aws sso login --sso-session "$AWS_SSO_SESSION"
fi
.venv/bin/python setup_profile.py
actual_account="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
if [[ "$actual_account" != "$expected_account" ]]; then
  echo "Account mismatch: expected $expected_account, received $actual_account. Stopping." >&2
  exit 1
fi
echo "Starting chatbot in AWS account $actual_account with profile $AWS_PROFILE"
export AWS_ACCOUNT_ID="$actual_account"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AGENT_OBSERVABILITY_ENABLED=true
export OTEL_PYTHON_DISTRO=aws_distro
export OTEL_PYTHON_CONFIGURATOR=aws_configurator
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://xray.$AWS_REGION.amazonaws.com/v1/traces"
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_RESOURCE_ATTRIBUTES="service.name=devoteam-pptx-agent,cloud.provider=aws,cloud.region=$AWS_REGION,cloud.account.id=$actual_account"
export OTEL_SERVICE_NAME=devoteam-pptx-agent
export OTEL_TRACES_SAMPLER=always_on
# Operational metadata only; do not capture prompts, tool source or file contents.
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
export TRACELOOP_TRACE_CONTENT=false
# Our manual spans cover AWS requests, avoiding auto-instrumented content capture.
export OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=botocore,bedrock
# Traces use the standard aws/spans destination. Application events use our own group.
unset OTEL_EXPORTER_OTLP_TRACES_HEADERS

case "${1:-}" in
  --setup-observability)
    exec .venv/bin/python setup_observability.py --enable ;;
  --verify-observability)
    exec .venv/bin/python setup_observability.py --verify ;;
  --smoke-observability)
    exec .venv/bin/opentelemetry-instrument .venv/bin/python setup_observability.py --smoke ;;
  '') ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac
.venv/bin/python setup_observability.py --bootstrap
.venv/bin/opentelemetry-instrument .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 &
app_pid=$!
trap 'kill -INT "$app_pid" 2>/dev/null || true; wait "$app_pid" 2>/dev/null || true; exit 0' INT TERM
if wait "$app_pid"; then
  exit 0
else
  app_status=$?
  exit "$app_status"
fi
