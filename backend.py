"""Bedrock Converse tool orchestration and AgentCore artifact generation."""
import base64
import json
import logging
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from jsonschema import validate
from observability import event, span, trace_agent
from rag import retrieve_context

LOG = logging.getLogger(__name__)
DECK_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['title', 'slides'],
    'properties': {
        'title': {'type': 'string', 'minLength': 1, 'maxLength': 100,
                  'description': 'Cover title: specific topic and useful outcome.'},
        'subtitle': {'type': 'string', 'maxLength': 180,
                     'description': 'Audience, purpose or context; do not repeat the title.'},
        'slides': {'type': 'array', 'minItems': 1, 'maxItems': 15,
                   'items': {'type': 'object', 'additionalProperties': False,
                             'required': ['title', 'bullets'], 'properties': {
                                 'title': {'type': 'string', 'minLength': 1, 'maxLength': 85,
                                           'description': 'One takeaway, preferably a 5–10 word conclusion.'},
                                 'bullets': {'type': 'array', 'minItems': 1, 'maxItems': 4,
                                             'items': {'type': 'string', 'minLength': 1,
                                                       'maxLength': 140}},
                                 'notes': {'type': 'string', 'maxLength': 2000,
                                           'description': '40–70 words of spoken explanation, examples and caveats beyond the cards.'}}}}
    }
}
SYSTEM = Path(__file__).with_name('presentation_prompt.txt').read_text(encoding='utf-8')
TOOLS = {'tools': [{'toolSpec': {
    'name': 'create_powerpoint',
    'description': ('Generate the complete PowerPoint in AgentCore Code Interpreter. '
                    'Supply a coherent, audience-specific deck with concise takeaway '
                    'titles, concrete cards and speaker notes. slides excludes the '
                    'automatically added cover: total slide count equals len(slides) + 1.'),
    'inputSchema': {'json': DECK_SCHEMA}
}}]}


def consume(response):
    """Read every event, including service errors and execution errors."""
    results = []
    stream = response['stream']
    try:
        for event in stream:
            if 'result' not in event:
                raise RuntimeError('AgentCore stream error: ' + json.dumps(event, default=str))
            result = event['result']
            structured = result.get('structuredContent', {})
            if result.get('isError') or structured.get('exitCode', 0) != 0:
                raise RuntimeError('AgentCore execution failed: ' + json.dumps(result, default=str))
            results.append(result)
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    if not results:
        raise RuntimeError('AgentCore returned no result.')
    return results


def interpreter_call(client, operation, **kwargs):
    attributes = {'rpc.system': 'aws-api', 'rpc.service': 'BedrockAgentCore',
                  'rpc.method': operation,
                  'aws.agentcore.interpreter.id': kwargs['codeInterpreterIdentifier']}
    if 'sessionId' in kwargs:
        attributes['aws.agentcore.session.id'] = kwargs['sessionId']
    if 'name' in kwargs:
        attributes['gen_ai.tool.name'] = kwargs['name']
    with span('agentcore.' + operation, **attributes) as active:
        response = getattr(client, operation)(**kwargs)
        request_id = response.get('ResponseMetadata', {}).get('RequestId', '')
        if active:
            active.set_attribute('aws.request_id', request_id)
            if response.get('sessionId'):
                active.set_attribute('aws.agentcore.session.id', response['sessionId'])
        if 'stream' in response:
            result = consume(response)
        else:
            result = response
        event('agentcore.response', operation=operation, request_id=request_id,
              interpreter_id=kwargs['codeInterpreterIdentifier'],
              session_id=response.get('sessionId', kwargs.get('sessionId', '')))
        return result


@trace_agent
def generate(client, identifier, deck, progress=None):
    def report(message):
        if progress:
            progress(message)

    validate(deck, DECK_SCHEMA)
    report('Starting AgentCore Code Interpreter session')
    session = interpreter_call(client, 'start_code_interpreter_session',
        codeInterpreterIdentifier=identifier, name='powerpoint-' + uuid4().hex[:12],
        sessionTimeoutSeconds=300)['sessionId']
    params = {'codeInterpreterIdentifier': identifier, 'sessionId': session}
    report(f'AgentCore session: {session} · interpreter: {identifier}')
    try:
        # Content remains data; the model cannot supply executable Python.
        encoded = base64.b64encode(json.dumps(deck).encode()).decode()
        source = Path(__file__).with_name('renderer.py').read_text()
        source += ('\nimport json, base64\n'
                   f'deck = json.loads(base64.b64decode({encoded!r}))\n'
                   'from pathlib import Path\n'
                   'Path("presentation.pptx").write_bytes(render(deck))\n'
                   'print("PowerPoint created")\n')
        report('Executing Python renderer in AgentCore (executeCode)')
        interpreter_call(client, 'invoke_code_interpreter', **params, name='executeCode',
                         arguments={'language': 'python', 'code': source})
        report('Downloading presentation.pptx from AgentCore (readFiles)')
        results = interpreter_call(client, 'invoke_code_interpreter',
            **params, name='readFiles', arguments={'paths': ['presentation.pptx']})
        for result in results:
            for block in result.get('content', []):
                resource = block.get('resource', {})
                blob = resource.get('blob') or block.get('data')
                if isinstance(blob, (bytes, bytearray)) and blob.startswith(b'PK'):
                    # boto3 already decodes blob fields. Never base64-decode twice.
                    report(f'Received PowerPoint from AgentCore: {len(blob):,} bytes')
                    return bytes(blob)
        shapes = []
        for result in results:
            for block in result.get('content', []):
                shapes.append({'type': block.get('type'), 'name': block.get('name'),
                               'mimeType': block.get('mimeType'),
                               'resourceType': block.get('resource', {}).get('type'),
                               'resourceName': block.get('resource', {}).get('name'),
                               'resourceMimeType': block.get('resource', {}).get('mimeType'),
                               'dataType': type(block.get('data')).__name__,
                               'blobType': type(block.get('resource', {}).get('blob')).__name__})
        raise RuntimeError('AgentCore readFiles returned no PowerPoint ZIP data. '
                           'Response content metadata: ' + json.dumps(shapes))
    finally:
        try:
            interpreter_call(client, 'stop_code_interpreter_session', **params)
            report('AgentCore session stopped')
        except Exception:
            LOG.exception('Failed to stop AgentCore session %s; its TTL is 300s', session)


def clients(region):
    config = Config(connect_timeout=10, read_timeout=300, retries={'max_attempts': 2})
    session = boto3.Session(region_name=region)
    agentcore = session.client('bedrock-agentcore', config=config)
    return (session.client('bedrock-runtime', config=config), agentcore)


@trace_agent
def respond(bedrock, agentcore, model_id, identifier, messages,
            force_generate=False, progress=None, session_id=None,
            knowledge_base=None, rag_control=None, rag_runtime=None):
    """Return updated transcript and artifacts; caller commits only on success."""
    history = list(messages)
    artifacts = []
    system = [{'text': SYSTEM}]
    sources = []
    if knowledge_base:
        if rag_control is None or rag_runtime is None:
            raise ValueError('Knowledge base clients are required when RAG is enabled.')
        with span('knowledge_base.retrieve'):
            context, sources = retrieve_context(rag_control, rag_runtime, knowledge_base,
                                                history, progress)
        system.append({'text': context})
    for round_number in range(4):
        tool_config = {'tools': TOOLS['tools']}
        if force_generate and round_number == 0:
            tool_config['toolChoice'] = {'tool': {'name': 'create_powerpoint'}}
        if progress:
            progress('Bedrock is preparing slide content')
        with span('chat ' + model_id, **{'gen_ai.operation.name': 'chat',
                                       'gen_ai.provider.name': 'aws.bedrock',
                                       'gen_ai.request.model': model_id}) as active:
            response = bedrock.converse(modelId=model_id, messages=history,
                                       system=system, toolConfig=tool_config,
                                       inferenceConfig={'maxTokens': 9000, 'temperature': .2})
            if active:
                for source, target in [('inputTokens', 'gen_ai.usage.input_tokens'),
                                       ('outputTokens', 'gen_ai.usage.output_tokens')]:
                    if source in response.get('usage', {}):
                        active.set_attribute(target, response['usage'][source])
        if response.get('stopReason') in ('max_tokens', 'content_filtered', 'guardrail_intervened'):
            raise RuntimeError('Bedrock could not complete the response: ' + response['stopReason'])
        message = response['output']['message']
        history.append(message)
        calls = [block['toolUse'] for block in message['content'] if 'toolUse' in block]
        if not calls:
            if force_generate and not artifacts:
                failures = [block['toolResult']['content'][0].get('text', 'Unknown tool error')
                            for item in history if item.get('role') == 'user'
                            for block in item.get('content', [])
                            if 'toolResult' in block and block['toolResult'].get('status') == 'error']
                detail = failures[-1] if failures else 'Bedrock returned no tool invocation.'
                raise RuntimeError('PowerPoint generation failed: ' + detail)
            return history, artifacts
        results = []
        for call in calls:
            try:
                if call['name'] != 'create_powerpoint':
                    raise ValueError('Unknown tool')
                trace = []
                def report(message):
                    trace.append(message)
                    if progress:
                        progress(message)
                binary = generate(agentcore, identifier, call['input'], progress=report)
                artifacts.append({'data': binary, 'title': call['input']['title'],
                                  'execution_trace': trace, 'sources': sources})
                result = {'status': 'success', 'content': [{'text': 'PowerPoint generated; download is available in the chat.'}]}
            except Exception as exc:
                if progress:
                    progress(f'PowerPoint generation failed: {exc}')
                result = {'status': 'error', 'content': [{'text': str(exc)[:2000]}]}
            results.append({'toolResult': {'toolUseId': call['toolUseId'], **result}})
        history.append({'role': 'user', 'content': results})
    raise RuntimeError('Tool round limit reached. Try fewer slides.')


def rag_clients(region):
    config = Config(connect_timeout=10, read_timeout=60, retries={'max_attempts': 2})
    session = boto3.Session(region_name=region)
    control = session.client('bedrock-agent', config=config)
    runtime = session.client('bedrock-agent-runtime', config=config)
    # Current Bedrock Retrieve API supports managedSearchConfiguration for
    # managed KBs, while some botocore releases still model only vector search.
    retrieval_shape = runtime.meta.service_model.operation_model('Retrieve').input_shape.members['retrievalConfiguration']
    if 'managedSearchConfiguration' not in retrieval_shape.members:
        retrieval_shape.members['managedSearchConfiguration'] = retrieval_shape.members['vectorSearchConfiguration']
    retrieval_shape.required_members = [name for name in retrieval_shape.required_members
                                        if name != 'vectorSearchConfiguration']
    retrieval_shape.metadata['required'] = retrieval_shape.required_members
    retrieval_shape._shape_model['required'] = retrieval_shape.required_members
    return control, runtime
