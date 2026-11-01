import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from backend import clients, respond, rag_clients
from rag import DEFAULT_KB

st.set_page_config(page_title='Chat to PowerPoint', page_icon='📊')
st.title('Chat to PowerPoint')
st.caption('Describe your presentation, refine it in chat, then download an editable PowerPoint.')
sample_prompt = Path(__file__).with_name('sample_prompt.txt').read_text(encoding='utf-8')
with st.expander('Try a sample: Orion Field Station RAG test'):
    st.text(sample_prompt)
    run_sample = st.button('Generate RAG test report')
with st.sidebar:
    st.header('AWS connection')
    region = st.text_input('AWS region', os.getenv('AWS_REGION', 'us-east-1'))
    default_model = ('eu.amazon.nova-pro-v1:0' if region.startswith('eu-')
                     else 'us.amazon.nova-pro-v1:0')
    model = st.text_input('Bedrock model or inference profile ID',
                          os.getenv('BEDROCK_MODEL_ID', default_model))
    st.caption('Amazon Nova Pro is the default. Anthropic models require a separate use-case submission.')
    interpreter = st.text_input('Code Interpreter ID', os.getenv('CODE_INTERPRETER_ID', 'aws.codeinterpreter.v1'))
    st.header('Knowledge base context')
    use_rag = st.checkbox('Use RAG for slide content', value=True)
    knowledge_base = st.text_input('Knowledge base ID, name or data source name',
                                  os.getenv('BEDROCK_KNOWLEDGE_BASE', DEFAULT_KB))
    kb_region = st.text_input('Knowledge base region', os.getenv('BEDROCK_KB_REGION', region))
    st.caption('Relevant passages are retrieved before planning slides. Sources appear below and in speaker notes.')
    st.caption('Uses your AWS credential chain, including AWS_PROFILE. No credentials are stored in this app.')
    if os.getenv('AGENT_OBSERVABILITY_ENABLED') == 'true':
        st.caption('AWS telemetry configured. Verify receipt with the observability check.')
        st.link_button('Open CloudWatch (current region)',
                       f'https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}')
    if st.button('New conversation'):
        st.session_state.clear()
        st.rerun()

for key, value in [('messages', []), ('artifacts', [])]:
    if key not in st.session_state:
        st.session_state[key] = value
if 'conversation_id' not in st.session_state:
    st.session_state.conversation_id = str(uuid4())

for message in st.session_state.messages:
    parts = [block['text'] for block in message['content'] if 'text' in block]
    if parts:
        with st.chat_message(message['role']):
            st.markdown('\n\n'.join(parts))

prompt = st.chat_input('Example: Create 6 slides explaining our cloud migration plan to executives')
generate_clicked = st.button('Generate PowerPoint', disabled=not st.session_state.messages)
force_generate = generate_clicked or run_sample
if run_sample:
    prompt = sample_prompt
elif generate_clicked:
    prompt = 'Generate the PowerPoint now using our conversation. Use the latest revisions.'
if prompt:
    if not model.strip():
        st.error('Enter a Bedrock model or inference profile ID in the sidebar.')
    else:
        with st.chat_message('user'):
            st.markdown(prompt)
        try:
            with st.status('Generating with Bedrock and AgentCore…', expanded=True) as status:
                bedrock, agentcore = clients(region)
                if use_rag and not knowledge_base.strip():
                    raise ValueError('Enter a knowledge base ID, name or data source name.')
                control, runtime = rag_clients(kb_region) if use_rag else (None, None)
                history, artifacts = respond(bedrock, agentcore, model, interpreter,
                    st.session_state.messages + [{'role': 'user', 'content': [{'text': prompt}]}],
                    force_generate=force_generate, progress=status.write,
                    session_id=st.session_state.conversation_id,
                    knowledge_base=knowledge_base if use_rag else None,
                    rag_control=control, rag_runtime=runtime)
                status.update(label='Request complete', state='complete')
            st.session_state.messages = history
            st.session_state.artifacts.extend(artifacts)
            st.rerun()
        except Exception as exc:
            st.error(f'Request failed: {exc}')

for i, artifact in enumerate(st.session_state.artifacts):
    if artifact.get('execution_trace'):
        with st.expander(f'AgentCore execution: {artifact["title"]}'):
            for step in artifact['execution_trace']:
                st.text(step)
    if artifact.get('sources'):
        with st.expander(f'Knowledge base sources: {artifact["title"]}'):
            for source in artifact['sources']:
                st.text(f"[{source['id']}] {source['location']}")
                st.write(source['text'])
    st.download_button(f'Download: {artifact["title"]}', artifact['data'],
                       file_name=f'presentation-{i + 1}.pptx',
                       mime='application/vnd.openxmlformats-officedocument.presentationml.presentation',
                       key=f'download-{i}')
