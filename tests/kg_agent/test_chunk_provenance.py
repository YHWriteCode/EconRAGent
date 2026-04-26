import asyncio

from lightrag_fork.lightrag import _build_chunking_result
from lightrag_fork.operate import chunking_by_token_size
from lightrag_fork.utils import compute_mdhash_id, generate_reference_list_from_chunks


class _CharacterTokenizer:
    def encode(self, content: str) -> list[str]:
        return list(content)

    def decode(self, tokens: list[str]) -> str:
        return "".join(tokens)


def test_small_non_pdf_document_stays_single_chunk_with_file_metadata():
    chunks = asyncio.run(
        _build_chunking_result(
            tokenizer=_CharacterTokenizer(),
            chunking_func=chunking_by_token_size,
            content="short note",
            doc_metadata={
                "source_label": "file",
                "source_filename": "brief.md",
                "file_format": "markdown",
            },
            segment_doc={
                "metadata": {"file_format": "markdown"},
                "segments": [
                    {
                        "content": "# Intro\nshort note",
                        "metadata": {"section_path": ["Intro"]},
                    }
                ],
            },
            split_by_character=None,
            split_by_character_only=False,
            chunk_overlap_token_size=2,
            chunk_token_size=100,
        )
    )

    assert len(chunks) == 1
    assert chunks[0]["content"] == "short note"
    assert chunks[0]["metadata"]["source_label"] == "file"
    assert chunks[0]["source_filename"] == "brief.md"


def test_pdf_segments_keep_page_number_metadata():
    chunks = asyncio.run(
        _build_chunking_result(
            tokenizer=_CharacterTokenizer(),
            chunking_func=chunking_by_token_size,
            content="page one\n\npage two",
            doc_metadata={
                "source_label": "file",
                "source_filename": "manual.pdf",
                "file_format": "pdf",
            },
            segment_doc={
                "metadata": {"file_format": "pdf"},
                "segments": [
                    {"content": "page one", "metadata": {"page_number": 1}},
                    {"content": "page two", "metadata": {"page_number": 2}},
                ],
            },
            split_by_character=None,
            split_by_character_only=False,
            chunk_overlap_token_size=2,
            chunk_token_size=100,
        )
    )

    assert [chunk["page_number"] for chunk in chunks] == [1, 2]
    assert all(chunk["metadata"]["source_label"] == "file" for chunk in chunks)


def test_chunk_ids_include_doc_context_for_identical_text():
    chunk_a = compute_mdhash_id("doc-a:0:identical", prefix="chunk-")
    chunk_b = compute_mdhash_id("doc-b:0:identical", prefix="chunk-")

    assert chunk_a != chunk_b


def test_reference_list_uses_page_and_section_metadata():
    references, chunks = generate_reference_list_from_chunks(
        [
            {
                "content": "A",
                "file_path": "manual.pdf",
                "metadata": {
                    "source_label": "file",
                    "page_number": 3,
                },
            },
            {
                "content": "B",
                "file_path": "guide.md",
                "metadata": {
                    "source_label": "file",
                    "section_path": ["Part I", "Chapter 2"],
                },
            },
        ]
    )

    assert references[0]["label"] == "manual.pdf - page 3"
    assert references[1]["label"] == "guide.md - Part I / Chapter 2"
    assert chunks[0]["reference_id"] == "1"
    assert chunks[1]["reference_id"] == "2"
