"""Tests for account and document isolation in local RAG retrieval."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from backend import bridge


class FakeSentenceTransformer:
    def __init__(self, *args, **kwargs):
        pass

    def encode(self, texts):
        vectors = []
        for text in texts:
            text_lower = str(text).lower()
            if 'loop' in text_lower:
                vectors.append([1.0, 0.0, 0.0])
            elif 'function' in text_lower:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class FakeCollection:
    """In-memory stand-in for the Chroma collection used by the tests."""

    def __init__(self):
        self.items = {}

    def _matches_where(self, metadata, where_filter):
        if not where_filter:
            return True
        if '$and' in where_filter:
            return all(
                self._matches_where(metadata, clause)
                for clause in where_filter['$and']
            )
        for key, condition in where_filter.items():
            value = metadata.get(key)
            if '$eq' in condition and value != condition['$eq']:
                return False
            if '$ne' in condition and value == condition['$ne']:
                return False
            if '$in' in condition and value not in condition['$in']:
                return False
            if '$nin' in condition and value in condition['$nin']:
                return False
        return True

    def get(self, include=None, where=None):
        ids = []
        documents = []
        metadatas = []
        for item_id, item in self.items.items():
            metadata = item['metadata']
            if not self._matches_where(metadata, where):
                continue
            ids.append(item_id)
            documents.append(item['document'])
            metadatas.append(metadata)
        return {'ids': ids, 'documents': documents, 'metadatas': metadatas}

    def delete(self, ids):
        for item_id in ids:
            self.items.pop(item_id, None)

    def upsert(self, ids, documents, metadatas, embeddings):
        for item_id, document, metadata, embedding in zip(
            ids,
            documents,
            metadatas,
            embeddings,
        ):
            self.items[item_id] = {
                'document': document,
                'metadata': metadata,
                'embedding': embedding,
            }

    def query(self, query_embeddings, n_results, where=None, include=None):
        query_vector = query_embeddings[0]
        matches = []
        for item_id, item in self.items.items():
            metadata = item['metadata']
            if not self._matches_where(metadata, where):
                continue
            distance = sum(
                (left - right) ** 2
                for left, right in zip(query_vector, item['embedding'])
            ) ** 0.5
            matches.append((distance, item['document'], metadata))

        matches.sort(key=lambda entry: entry[0])
        top_matches = matches[:n_results]
        result = {'documents': [[]], 'metadatas': [[]], 'distances': [[]]}
        for distance, document, meta in top_matches:
            result['documents'][0].append(document)
            result['metadatas'][0].append(meta)
            result['distances'][0].append(distance)
        return result


class FakePersistentClient:
    def __init__(self, path):
        self.path = path
        self._collection = FakeCollection()

    def get_or_create_collection(self, name):
        return self._collection


class RetrievalScopeTest(unittest.TestCase):
    def setUp(self):
        self.mongo_environment = patch.dict(os.environ, {'MONGODB_URI': ''})
        self.mongo_environment.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self.temp_dir.name) / 'raw'
        user_dir = self.raw_dir / 'user-1'
        user_dir.mkdir(parents=True)
        (user_dir / 'doc_a.txt').write_text(
            'Loop exercise. Problem 5 uses for loops and while loops.',
            encoding='utf-8',
        )
        (user_dir / 'doc_b.txt').write_text(
            'Function exercise. Problem 7 uses reusable helper functions.',
            encoding='utf-8',
        )
        self.chroma_dir = Path(self.temp_dir.name) / 'chroma'

        self.patches = [
            patch.object(bridge, 'RAW_DIR', self.raw_dir),
            patch.object(bridge, 'CHROMA_DIR', self.chroma_dir),
            patch.object(
                bridge,
                'create_embedding_model',
                side_effect=lambda _: FakeSentenceTransformer(),
            ),
            patch.object(bridge, 'create_chroma_client', side_effect=FakePersistentClient),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp_dir.cleanup()
        self.mongo_environment.stop()

    def test_query_scopes_to_active_document_and_user(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        rag.rebuild_index(user_id='user-1')

        document_id = rag.document_id_for('doc_a.txt', user_id='user-1')
        matches = rag.retrieve(
            'loop exercise',
            user_id='user-1',
            document_id=document_id,
            limit=5,
            max_distance=10,
        )

        self.assertTrue(matches)
        self.assertEqual({match['source'] for match in matches}, {'doc_a.txt'})

    def test_notebook_extraction_indexes_markdown_and_code_but_not_outputs(self):
        notebook = {
            'cells': [
                {
                    'cell_type': 'markdown',
                    'source': ['# Project notes\n', 'The app uses vector search.'],
                },
                {
                    'cell_type': 'code',
                    'source': ['def retrieve():\n', '    return "Atlas"'],
                    'outputs': [{'data': {'text/plain': 'secret runtime output'}}],
                },
            ],
        }

        text = bridge.RAGService().read_uploaded_document(
            'rag-notes.ipynb',
            json.dumps(notebook).encode('utf-8'),
        )

        self.assertIn('The app uses vector search.', text)
        self.assertIn('def retrieve():', text)
        self.assertNotIn('secret runtime output', text)

    def test_notebook_extraction_reports_invalid_json(self):
        with self.assertRaisesRegex(ValueError, 'not valid UTF-8 JSON'):
            bridge.RAGService().read_uploaded_document(
                'broken.ipynb',
                b'not a notebook',
            )

    def test_notebook_question_uses_extracted_notebook_context(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        notebook = {
            'cells': [
                {'cell_type': 'markdown', 'source': ['Notebook about retrieval.']},
                {'cell_type': 'code', 'source': ['def retrieve():\n', '    return "chunks"']},
            ],
        }
        path = self.raw_dir / 'user-1' / 'retrieval.ipynb'
        path.write_text(json.dumps(notebook), encoding='utf-8')
        rag.rebuild_index(user_id='user-1')

        with patch.object(rag, 'has_any_provider_key', return_value=True), \
                patch.object(
                    rag,
                    'call_providers',
                    return_value=('The notebook retrieves chunks.', 'gemini'),
                ) as call_providers:
            result = rag.ask('Explain this notebook', user_id='user-1')

        self.assertEqual(result['mode'], 'rag')
        self.assertEqual(result['sources'], ['retrieval.ipynb'])
        self.assertIn('def retrieve():', call_providers.call_args.args[0])

    def test_general_knowledge_question_uses_chat_without_document_retrieval(self):
        rag = bridge.RAGService()
        self.assertFalse(rag.needs_documents('What is the capital of Greece?'))
        self.assertFalse(rag.needs_documents('What is an API?'))
        self.assertTrue(rag.needs_documents('Explain the API in this codebase'))
        with patch.object(rag, 'has_any_provider_key', return_value=True), \
                patch.object(
                    rag,
                    'call_providers',
                    return_value=('Athens is the capital of Greece.', 'gemini'),
                ) as call_providers, \
                patch.object(rag, 'retrieve') as retrieve, \
                patch.object(rag, 'keyword_search') as keyword_search:
            result = rag.ask('What is the capital of Greece?')

        self.assertEqual(result['mode'], 'chat')
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['answer'], 'Athens is the capital of Greece.')
        call_providers.assert_called_once()
        self.assertIn('User: What is the capital of Greece?', call_providers.call_args.args[0])
        retrieve.assert_not_called()
        keyword_search.assert_not_called()

    def test_explicit_uploaded_file_question_uses_document_context(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        with patch.object(rag, 'has_any_provider_key', return_value=True), \
                patch.object(
                    rag,
                    'call_providers',
                    return_value=('The file describes loop exercises.', 'gemini'),
                ) as call_providers:
            result = rag.ask(
                        'What does this file doc_a.txt say about loop exercises?',
                user_id='user-1',
            )

        self.assertEqual(result['mode'], 'rag')
        self.assertTrue(result['sources'])
        self.assertIn('Context:', call_providers.call_args.args[0])
        self.assertIn('Loop exercise.', call_providers.call_args.args[0])

    def test_same_filename_and_content_do_not_overwrite_another_users_chunks(self):
        rag = bridge.RAGService()
        rag.rebuild_index(user_id='user-1')

        user_two_directory = self.raw_dir / 'user-2'
        user_two_directory.mkdir()
        (user_two_directory / 'doc_a.txt').write_text(
            (self.raw_dir / 'user-1' / 'doc_a.txt').read_text(encoding='utf-8'),
            encoding='utf-8',
        )
        rag.rebuild_index(user_id='user-2')

        user_one_matches = rag.retrieve(
            'loop exercise',
            user_id='user-1',
            document_id=rag.document_id_for('doc_a.txt', user_id='user-1'),
            max_distance=10,
        )
        user_two_matches = rag.retrieve(
            'loop exercise',
            user_id='user-2',
            document_id=rag.document_id_for('doc_a.txt', user_id='user-2'),
            max_distance=10,
        )

        self.assertEqual({match['source'] for match in user_one_matches}, {'doc_a.txt'})
        self.assertEqual({match['source'] for match in user_two_matches}, {'doc_a.txt'})

    def test_rag_service_defers_model_and_database_initialization(self):
        with patch.object(bridge, 'create_embedding_model') as embedding_factory, \
                patch.object(bridge, 'create_chroma_client') as chroma_factory:
            rag = bridge.RAGService()
            rag.load_documents(user_id='user-with-no-files')

        embedding_factory.assert_not_called()
        chroma_factory.assert_not_called()

    def test_rebuild_writes_document_chunks_to_atlas_and_keeps_chroma(self):
        class AtlasEmbedding:
            def encode(self, texts):
                return [[1.0] + [0.0] * 383 for _ in texts]

        rag = bridge.RAGService()
        rag._embedding_model = AtlasEmbedding()
        chroma_query = Mock()
        rag.collection.query = chroma_query
        atlas_chunks = MagicMock()
        user_documents = MagicMock()
        database = MagicMock()
        database.__getitem__.side_effect = lambda name: {
            'document_chunks': atlas_chunks,
            'user_documents': user_documents,
        }[name]
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test.invalid'}), \
                patch.object(bridge, 'mongo_database', return_value=database):
            result = rag.rebuild_index(user_id='user-1')

        self.assertGreater(result['chunks'], 0)
        atlas_chunks.bulk_write.assert_called_once()
        atlas_chunks.delete_many.assert_called_once()
        self.assertGreater(len(rag.collection.items), 0)
        user_documents.update_one.assert_called()

    def test_atlas_vector_retrieval_prefilters_by_user_and_document(self):
        class AtlasEmbedding:
            def encode(self, texts):
                return [[1.0] + [0.0] * 383 for _ in texts]

        rag = bridge.RAGService()
        rag._embedding_model = AtlasEmbedding()
        chroma_query = Mock()
        rag.collection.query = chroma_query
        atlas_chunks = MagicMock()
        atlas_chunks.aggregate.return_value = [{
            'text': 'Atlas stores semantically indexed documentation.',
            'source': 'notes.txt',
            'chunk': 2,
            'score': 0.95,
        }]
        database = MagicMock()
        database.__getitem__.return_value = atlas_chunks
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test.invalid'}), \
                patch.object(bridge, 'mongo_database', return_value=database):
            matches = rag.retrieve(
                'How does Atlas search?',
                user_id='user-1',
                document_id='doc-1',
            )

        self.assertEqual(matches[0]['source'], 'notes.txt')
        self.assertEqual(matches[0]['chunk'], 2)
        pipeline = atlas_chunks.aggregate.call_args.args[0]
        self.assertEqual(pipeline[0]['$vectorSearch']['index'], 'devcompass_vector')
        self.assertEqual(
            pipeline[0]['$vectorSearch']['filter'],
            {
                'user_id': {'$eq': 'user-1'},
                'document_id': {'$eq': 'doc-1'},
            },
        )
        chroma_query.assert_not_called()

    def test_atlas_vector_retrieval_falls_back_to_chroma_when_no_match(self):
        class AtlasEmbedding:
            def encode(self, texts):
                return [[1.0] + [0.0] * 383 for _ in texts]

        rag = bridge.RAGService()
        rag._embedding_model = AtlasEmbedding()
        chroma_query = Mock(return_value={
            'documents': [['local passage']],
            'metadatas': [[{'source': 'local.txt', 'chunk': 0}]],
            'distances': [[0.25]],
        })
        rag.collection.query = chroma_query
        atlas_chunks = MagicMock()
        atlas_chunks.aggregate.return_value = []
        database = MagicMock()
        database.__getitem__.return_value = atlas_chunks
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test.invalid'}), \
                patch.object(bridge, 'mongo_database', return_value=database):
            matches = rag.retrieve('query', user_id='user-1')

        self.assertEqual(matches[0]['source'], 'local.txt')
        chroma_query.assert_called_once()

    def test_unreadable_documents_are_logged_without_skipping_other_files(self):
        rag = bridge.RAGService()
        with patch.object(
            rag,
            'read_document',
            side_effect=[PermissionError('access denied'), 'Readable document'],
        ):
            with self.assertLogs('backend.bridge', level='WARNING') as captured:
                documents = rag.load_documents(user_id='user-1')

        self.assertIn('doc_a.txt', captured.output[0])
        self.assertEqual([document['source'] for document in documents], ['doc_b.txt'])

    def test_document_files_are_isolated_between_account_folders(self):
        rag = bridge.RAGService()

        user_documents = rag.load_documents(user_id='user-1')
        self.assertEqual(
            {document['source'] for document in user_documents},
            {'doc_a.txt', 'doc_b.txt'},
        )
        self.assertEqual(rag.load_documents(user_id='user-2'), [])

    def test_keyword_search_does_not_cross_file_boundaries(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        rag.rebuild_index(user_id='user-1')

        document_id = rag.document_id_for('doc_a.txt', user_id='user-1')
        matches = rag.keyword_search(
            'reusable helper functions',
            user_id='user-1',
            document_id=document_id,
            limit=5,
        )

        self.assertEqual(matches, [])

    def test_explicit_file_name_in_question_prioritizes_that_document(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        rag.rebuild_index(user_id='user-1')

        active_document_id = rag.document_id_for('doc_a.txt', user_id='user-1')
        matches = rag.resolve_ambiguous_file_question(
            'doc_b.txt list what is in this file',
            user_id='user-1',
            document_id=active_document_id,
        )

        self.assertTrue(matches)
        self.assertEqual(matches[0]['source'], 'doc_b.txt')

    def test_document_library_lists_indexed_files_and_active_source(self):
        rag = bridge.RAGService()
        rag.default_user_id = 'user-1'
        rag.rebuild_index(user_id='user-1')

        with patch.object(bridge, 'rag_service', rag), \
                patch.object(bridge, 'request_user', return_value={'_id': 'user-1'}):
            response = bridge.list_documents(Mock(cookies={}))

        filenames = {item['filename'] for item in response['documents']}
        self.assertEqual(filenames, {'doc_a.txt', 'doc_b.txt'})
        self.assertTrue(all('chunks' not in item for item in response['documents']))
        active_count = sum(item['active'] for item in response['documents'])
        self.assertEqual(active_count, 1)


if __name__ == '__main__':
    unittest.main()
