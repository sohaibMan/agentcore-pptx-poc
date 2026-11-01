"""Enable/check US East (N. Virginia) CloudWatch resources for the local PoC. No IAM role changes."""
import argparse
import json
import os
import subprocess
import sys
import time

import boto3
from botocore.config import Config

from observability import LOG_GROUP, SERVICE, event, span

ACCOUNT = os.environ['AWS_ACCOUNT_ID']
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 1})


def client(session, service):
    return session.client(service, config=CLIENT_CONFIG)


def resource_policy(region):
    return {'Version': '2012-10-17', 'Statement': [{
        'Sid': 'TransactionSearchXRayAccess', 'Effect': 'Allow',
        'Principal': {'Service': 'xray.amazonaws.com'}, 'Action': 'logs:PutLogEvents',
        'Resource': [f'arn:aws:logs:{region}:{ACCOUNT}:log-group:aws/spans:*',
                     f'arn:aws:logs:{region}:{ACCOUNT}:log-group:/aws/application-signals/data:*'],
        'Condition': {'ArnLike': {'aws:SourceArn': f'arn:aws:xray:{region}:{ACCOUNT}:*'},
                      'StringEquals': {'aws:SourceAccount': ACCOUNT}}
    }]}


def ensure_group(logs, name):
    try:
        logs.create_log_group(logGroupName=name)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass


def configure(session):
    region = session.region_name
    # Validate identity before performing any account-level changes.
    actual = client(session, 'sts').get_caller_identity()['Account']
    if actual != ACCOUNT:
        raise RuntimeError(f'Expected sandbox account {ACCOUNT}, got {actual}; no changes made.')
    logs = client(session, 'logs')
    xray = client(session, 'xray')
    ensure_group(logs, LOG_GROUP)
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=14)
    # Preserve retention and indexing settings of the shared aws/spans group.
    logs.put_resource_policy(policyName='DevoteamPptxTransactionSearch',
                              policyDocument=json.dumps(resource_policy(region)))
    xray.update_trace_segment_destination(Destination='CloudWatchLogs')
    print(f'CloudWatch Transaction Search configured in {ACCOUNT}/{region}.')
    print('Existing indexing sampling is unchanged. Initial activation can take 10 minutes.')


def check(session, verify_delivery=False, trace_ids=None):
    account = client(session, 'sts').get_caller_identity()['Account']
    if account != ACCOUNT:
        raise RuntimeError(f'Wrong AWS account: {account}')
    destination = client(session, 'xray').get_trace_segment_destination()
    print('X-Ray destination:', destination.get('Destination'),
          'status:', destination.get('Status'))
    if destination.get('Destination') != 'CloudWatchLogs':
        raise RuntimeError('Transaction Search is not enabled. Run with --enable.')
    if verify_delivery:
        logs = client(session, 'logs')
        query = dict(logGroupName='aws/spans', startTime=int((time.time() - 1800) * 1000),
                     filterPattern=f'"{SERVICE}"', limit=20)
        count = 0
        while True:
            response = logs.filter_log_events(**query)
            events = response.get('events', [])
            if trace_ids:
                events = [item for item in events if any(
                    trace_id in item.get('message', '').replace('-', '')
                    for trace_id in trace_ids)]
            count += len(events)
            token = response.get('nextToken')
            if count or not token or token == query.get('nextToken'):
                break
            query['nextToken'] = token
        print(f'Found {count} matching span records in aws/spans in the past 30 minutes.')
        if not count:
            raise RuntimeError('No spans received yet. Run --smoke with ADOT instrumentation, '
                               'wait for delivery, then run --verify again. Check xray:PutTraceSegments permission.')


def ensure_setup(session):
    """Reuse a completed setup; repair missing resources without a local marker."""
    try:
        check(session)
        logs = client(session, 'logs')
        policies = logs.describe_resource_policies()['resourcePolicies']
        if not any(p['policyName'] == 'DevoteamPptxTransactionSearch' for p in policies):
            configure(session)
            return
        groups = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)['logGroups']
        if not any(g['logGroupName'] == LOG_GROUP for g in groups):
            configure(session)
            return
        print('Reusing existing CloudWatch observability setup.')
    except RuntimeError as exc:
        if 'Transaction Search is not enabled' not in str(exc):
            raise
        configure(session)


def smoke():
    from opentelemetry import trace
    from observability import current_trace_id
    if os.getenv('AGENT_OBSERVABILITY_ENABLED') != 'true':
        raise RuntimeError('Launch via bash run.sh with ADOT instrumentation.')
    with span('observability.smoke', **{'gen_ai.agent.name': SERVICE}):
        trace_id = current_trace_id()
        if not trace_id:
            raise RuntimeError('No recording tracer. Launch with opentelemetry-instrument.')
        event('observability.smoke', smoke=True)
        print('Smoke trace ID:', trace_id, flush=True)
    # Some ADOT 0.17 providers return False when an asynchronous export has
    # already logged its own HTTP error. Server-side receipt below is the check.
    trace.get_tracer_provider().force_flush(timeout_millis=10000)
    return trace_id


def run_smoke_process():
    """Initialize ADOT only after CloudWatch has activated the X-Ray destination."""
    command = [sys.executable.replace('/python', '/opentelemetry-instrument'),
               sys.executable, str(__file__), '--smoke']
    completed = subprocess.run(command, check=False, text=True, capture_output=True,
                               env=os.environ.copy())
    output = completed.stdout + completed.stderr
    print(output, end='', flush=True)
    if completed.returncode:
        raise RuntimeError(f'Instrumented smoke process failed (exit {completed.returncode}).')
    for line in output.splitlines():
        if line.startswith('Smoke trace ID: '):
            return line.split(': ', 1)[1].strip()
    raise RuntimeError('Instrumented smoke process did not return a trace ID.')


def bootstrap(session, timeout=660):
    ensure_setup(session)
    deadline = time.monotonic() + timeout
    xray = client(session, 'xray')
    while True:
        destination = xray.get_trace_segment_destination()
        name, status = destination.get('Destination'), destination.get('Status')
        print(f'Waiting for X-Ray destination: {name} ({status})', flush=True)
        if name == 'CloudWatchLogs' and status == 'ACTIVE':
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f'X-Ray destination did not become ACTIVE within {timeout}s '
                               f'(last state: {name}/{status}). The chatbot was not started.')
        time.sleep(15)
    print('Sending a test trace and verifying AWS receipt. First activation may take '
          'up to 10 minutes.', flush=True)
    deadline = time.monotonic() + timeout
    trace_ids = []
    next_smoke = 0
    while True:
        now = time.monotonic()
        if now >= next_smoke:
            trace_ids.append(run_smoke_process())
            next_smoke = now + 60
        try:
            check(session, verify_delivery=True, trace_ids=trace_ids)
            print('AWS received this startup test trace. Starting the chatbot.', flush=True)
            return
        except RuntimeError as exc:
            if 'No spans received yet' not in str(exc) and 'Transaction Search is not enabled' not in str(exc):
                raise
        if time.monotonic() >= deadline:
            raise RuntimeError('AWS has not received a startup trace within 11 minutes. '
                               'Check xray:PutTraceSegments permissions and exporter errors above. '
                               'The chatbot was not started; fix the error and rerun bash run.sh.')
        print('Waiting for CloudWatch trace delivery; retrying in 15 seconds…', flush=True)
        time.sleep(15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--enable', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    session = boto3.Session(region_name=os.getenv('AWS_REGION', 'us-east-1'))
    if args.enable:
        configure(session)
    elif args.bootstrap:
        bootstrap(session)
    elif args.smoke:
        smoke()
        print('Export attempted; use --verify-observability to confirm server-side receipt.')
    else:
        check(session, args.verify)


if __name__ == '__main__':
    main()
