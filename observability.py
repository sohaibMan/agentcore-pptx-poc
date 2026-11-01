"""ADOT spans plus payload-free operational events in CloudWatch Logs."""
import functools
import json
import logging
import os
import threading
import time
from contextlib import contextmanager

import boto3
from botocore.config import Config

LOG = logging.getLogger(__name__)
SERVICE = 'devoteam-pptx-agent'
LOG_GROUP = '/devoteam/agentcore-pptx'
_logs = None
_lock = threading.Lock()
_stream = 'app-' + str(os.getpid()) + '-' + str(int(time.time()))


def enabled():
    return os.getenv('AGENT_OBSERVABILITY_ENABLED', '').lower() == 'true'


def current_trace_id():
    if not enabled():
        return ''
    from opentelemetry import trace
    ctx = trace.get_current_span().get_span_context()
    return f'{ctx.trace_id:032x}' if ctx.is_valid else ''


def event(name, **attributes):
    if not enabled():
        return
    global _logs
    record = {'event': name, 'service': SERVICE, 'trace_id': current_trace_id(),
              'timestamp': int(time.time() * 1000), **attributes}
    try:
        with _lock:
            if _logs is None:
                client = boto3.client('logs', region_name=os.getenv('AWS_REGION', 'us-east-1'),
                                      config=Config(connect_timeout=5, read_timeout=5,
                                                    retries={'max_attempts': 1}))
                try:
                    client.create_log_stream(logGroupName=LOG_GROUP, logStreamName=_stream)
                except client.exceptions.ResourceAlreadyExistsException:
                    pass
                _logs = client
            _logs.put_log_events(logGroupName=LOG_GROUP, logStreamName=_stream,
                                 logEvents=[{'timestamp': record['timestamp'],
                                             'message': json.dumps(record)}])
    except Exception:
        LOG.exception('CloudWatch event delivery failed; generation can continue')


@contextmanager
def span(name, **attributes):
    if not enabled():
        yield None
        return
    from opentelemetry import trace
    # Errors are recorded by type only: AWS errors can contain request content.
    from opentelemetry.trace import Status, StatusCode
    with trace.get_tracer(SERVICE).start_as_current_span(
            name, attributes=attributes, record_exception=False,
            set_status_on_exception=False) as active:
        event(name + '.started', **attributes)
        try:
            yield active
        except Exception as exc:
            active.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            active.set_attribute('error.type', type(exc).__name__)
            event(name + '.failed', error_type=type(exc).__name__, **attributes)
            raise
        else:
            event(name + '.completed', **attributes)


def trace_agent(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            is_tool = function.__name__ == 'generate'
            with span('execute_tool create_powerpoint' if is_tool else 'invoke_agent ' + SERVICE, **{
                'gen_ai.operation.name': 'execute_tool' if is_tool else 'invoke_agent',
                'gen_ai.tool.name': 'create_powerpoint' if is_tool else '',
                'gen_ai.agent.name': SERVICE,
                'gen_ai.conversation.id': kwargs.get('session_id', ''),
                'session.id': kwargs.get('session_id', '')
            }):
                trace_id = current_trace_id()
                if trace_id and kwargs.get('progress'):
                    kwargs['progress']('CloudWatch trace ID: ' + trace_id)
                return function(*args, **kwargs)
        finally:
            if enabled():
                from opentelemetry import trace
                provider = trace.get_tracer_provider()
                if hasattr(provider, 'force_flush'):
                    provider.force_flush(timeout_millis=10000)
    return wrapped


