import unittest
from unittest.mock import Mock, patch

from rag import DEFAULT_KB, resolve, retrieve_context
from backend import respond


class RagTests(unittest.TestCase):
    def test_data_source_resolution_and_filtered_retrieval(self):
        control = Mock()
        control.list_knowledge_bases.side_effect = [
            {'knowledgeBaseSummaries': [], 'nextToken': 'page2'},
            {'knowledgeBaseSummaries': [{'name': 'parent', 'knowledgeBaseId': 'ABCDEFGHIJ'}]}]
        control.list_data_sources.return_value = {'dataSourceSummaries': [
            {'name': DEFAULT_KB, 'dataSourceId': 'SOURCE1234'}]}
        runtime = Mock()
        runtime.retrieve.return_value = {'retrievalResults': [
            {'content': {'text': 'Evidence'}, 'location': {'s3Location': {'uri': 's3://kb/report'}}},
            {'content': {'byteContent': 'ignored'}}]}
        messages = [{'role': 'user', 'content': [{'text': 'Cloud migration'}]},
                    {'role': 'user', 'content': [{'text': 'Generate now'}]}]
        context, sources = retrieve_context(control, runtime, DEFAULT_KB, messages)
        request = runtime.retrieve.call_args.kwargs
        self.assertEqual(request['knowledgeBaseId'], 'ABCDEFGHIJ')
        self.assertEqual(request['retrievalConfiguration']['vectorSearchConfiguration']['filter'],
                         {'equals': {'key': 'x-amz-bedrock-kb-data-source-id', 'value': 'SOURCE1234'}})
        self.assertIn('Cloud migration', request['retrievalQuery']['text'])
        self.assertIn('Evidence', context)
        self.assertEqual(sources[0]['location'], 's3://kb/report')

    def test_ambiguous_source_fails(self):
        control = Mock()
        control.list_knowledge_bases.return_value = {'knowledgeBaseSummaries': [
            {'name': 'one', 'knowledgeBaseId': '1111111111'},
            {'name': 'two', 'knowledgeBaseId': '2222222222'}]}
        control.list_data_sources.return_value = {'dataSourceSummaries': [
            {'name': DEFAULT_KB, 'dataSourceId': 'SOURCE1234'}]}
        with self.assertRaisesRegex(ValueError, 'found 2'):
            resolve(control, DEFAULT_KB)

    def test_context_is_passed_to_model_without_polluting_history(self):
        bedrock = Mock()
        bedrock.converse.return_value = {'output': {'message': {
            'role': 'assistant', 'content': [{'text': 'Ready'}]}}}
        messages = [{'role': 'user', 'content': [{'text': 'Topic'}]}]
        with patch('backend.retrieve_context', return_value=('Retrieved evidence', [])):
            history, artifacts = respond(bedrock, Mock(), 'model', 'interpreter', messages,
                knowledge_base=DEFAULT_KB, rag_control=Mock(), rag_runtime=Mock())
        self.assertEqual(bedrock.converse.call_args.kwargs['system'][1],
                         {'text': 'Retrieved evidence'})
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(artifacts, [])

    def test_retrieval_failure_prevents_ungrounded_generation(self):
        bedrock = Mock()
        with patch('backend.retrieve_context', side_effect=RuntimeError('Access denied')):
            with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                respond(bedrock, Mock(), 'model', 'interpreter', [],
                    knowledge_base=DEFAULT_KB, rag_control=Mock(), rag_runtime=Mock())
        bedrock.converse.assert_not_called()


if __name__ == '__main__':
    unittest.main()
