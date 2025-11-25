# Copyright 2025 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ruff: noqa: PLC0415,UP007,UP035,UP006,E712
# SPDX-License-Identifier: Apache-2.0
from typing import Iterator, List, Tuple, Dict
import logging

from kfp import compiler, dsl
from kfp.kubernetes import add_node_selector_json, add_toleration_json

PYTHON_BASE_IMAGE = "registry.redhat.io/ubi9/python-312@sha256:e80ff3673c95b91f0dafdbe97afb261eab8244d7fd8b47e20ffcbcfee27fb168"

# Workbench Runtime Image: Pytorch with CUDA and Python 3.11 (UBI 9)
# The images for each release can be found in
# https://github.com/red-hat-data-services/rhoai-disconnected-install-helper/blob/main/rhoai-2.21.md
PYTORCH_CUDA_IMAGE = "quay.io/modh/odh-pipeline-runtime-pytorch-cuda-py311-ubi9@sha256:4706be608af3f33c88700ef6ef6a99e716fc95fc7d2e879502e81c0022fd840e"

_log = logging.getLogger(__name__)


@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
    packages_to_install=["llama-stack-client", "fire", "requests"],
)
def register_vector_db(
    service_url: str,
    vector_db_id: str,
    embed_model_id: str,
):
    from llama_stack_client import LlamaStackClient

    client = LlamaStackClient(base_url=service_url)

    models = client.models.list()
    matching_model = next(
        (m for m in models if m.provider_resource_id == embed_model_id), None
    )

    if not matching_model:
        raise ValueError(
            f"Model with ID '{embed_model_id}' not found on LlamaStack server."
        )

    if matching_model.model_type != "embedding":
        raise ValueError(f"Model '{embed_model_id}' is not an embedding model")

    embedding_dimension = matching_model.metadata["embedding_dimension"]

    _ = client.vector_dbs.register(
        vector_db_id=vector_db_id,
        embedding_model=matching_model.identifier,
        embedding_dimension=embedding_dimension,
        provider_id="milvus",
    )
    print(
        f"Registered vector DB '{vector_db_id}' with embedding model '{embed_model_id}'."
    )


@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
    packages_to_install=["requests"],
)
def import_spreadsheet_files(
    base_url: str,
    spreadsheet_filenames: str,
    output_path: dsl.OutputPath("input-spreadsheets"),
):
    import os
    import requests
    import shutil

    os.makedirs(output_path, exist_ok=True)
    filenames = [f.strip() for f in spreadsheet_filenames.split(",") if f.strip()]

    for filename in filenames:
        url = f"{base_url.rstrip('/')}/{filename}"
        file_path = os.path.join(output_path, filename)

        try:
            with requests.get(url, stream=True, timeout=30) as response:
                response.raise_for_status()
                with open(file_path, "wb") as f:
                    shutil.copyfileobj(response.raw, f)
            print(f"Downloaded {filename}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to download {filename}: {e}, skipping.")


@dsl.component(
    base_image=PYTHON_BASE_IMAGE,
)
def create_spreadsheet_splits(
    input_path: dsl.InputPath("input-spreadsheets"),
    num_splits: int,
) -> List[List[str]]:
    import pathlib

    # Split our entire directory of spreadsheet files into n batches, where n == num_splits
    # Support common formats
    spreadsheet_extensions = ["*.csv", "*.xlsx", "*.xls", "*.xlsm"]
    all_spreadsheets = []

    input_dir = pathlib.Path(input_path)
    for ext in spreadsheet_extensions:
        all_spreadsheets.extend([path.name for path in input_dir.glob(ext)])

    splits = [
        batch
        for batch in (all_spreadsheets[i::num_splits] for i in range(num_splits))
        if batch
    ]
    return splits or [[]]


@dsl.component(
    base_image=PYTORCH_CUDA_IMAGE,
    packages_to_install=[
        "docling",
        "docling-core",
        "transformers",
        "sentence-transformers",
        "llama-stack",
        "llama-stack-client",
        "pymilvus",
        "fire",
        "pandas>=2.3.0",
        "openpyxl>=3.1.5",
    ],
)
def docling_convert_and_ingest_spreadsheets(
    input_path: dsl.InputPath("input-spreadsheets"),
    spreadsheet_split: List[str],
    embed_model_id: str,
    max_tokens: int,
    service_url: str,
    vector_db_id: str,
):
    import pathlib
    import pandas as pd
    from docling.datamodel.base_models import ConversionStatus
    from docling.datamodel.document import ConversionResult
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc.document import DoclingDocument
    import shutil
    import tempfile
    import uuid

    from transformers import AutoTokenizer
    from sentence_transformers import SentenceTransformer
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    import logging
    from llama_stack_client import LlamaStackClient
    import json

    _log = logging.getLogger(__name__)

    # Create a truly local temp directory for processing
    local_processing_dir = pathlib.Path(tempfile.mkdtemp(prefix="docling-local-"))
    _log.info(f"Local processing directory: {local_processing_dir}")

    def convert_excel_to_csv(
        input_spreadsheet_files: List[pathlib.Path], output_path: pathlib.Path
    ) -> List[pathlib.Path]:
        processed_csv_files = []

        for file_path in input_spreadsheet_files:
            if not file_path.exists():
                _log.info(f"Skipping missing file: {file_path}")
                continue

            if file_path.suffix.lower() == ".csv":
                new_path = output_path / file_path.name
                try:
                    # First, try to read it as a normal CSV. 'infer' handles uncompressed files.
                    df = pd.read_csv(file_path, compression="infer", engine="python")
                    _log.info(f"Read {file_path.name} as a standard CSV.")

                except (UnicodeDecodeError, EOFError):
                    # If it fails with a decoding error, it's likely a misnamed compressed file.
                    _log.warning(
                        f"Standard read failed for {file_path.name}. Attempting gzip decompression."
                    )
                    try:
                        # Second, try reading it again, but force gzip decompression.
                        df = pd.read_csv(file_path, compression="gzip", engine="python")
                        _log.info(
                            f"Successfully read {file_path.name} with forced gzip."
                        )
                    except Exception as e:
                        _log.error(
                            f"Could not read {file_path.name} with any method. Error: {e}"
                        )
                        continue

                df.to_csv(new_path, index=False)
                processed_csv_files.append(new_path)

            # Check if the file is an Excel format
            elif file_path.suffix.lower() in [".xlsx", ".xls", ".xlsm"]:
                _log.info(f"Converting {file_path.name} to CSV format...")

                try:
                    # Use pandas to read all sheets from the Excel file
                    excel_sheets = pd.read_excel(file_path, sheet_name=None)

                    for sheet_name, df in excel_sheets.items():
                        new_csv_filename = f"{file_path.stem}_{sheet_name}.csv"
                        new_csv_path = output_path / new_csv_filename
                        df.to_csv(new_csv_path, index=False, header=True)
                        processed_csv_files.append(new_csv_path)
                        _log.info(
                            f"Successfully converted sheet '{sheet_name}' to '{new_csv_path.name}'"
                        )

                except Exception as e:
                    _log.error(f"Excel conversion failed for {file_path.name}: {e}")
                    continue
            else:
                _log.info(f"Skipping unsupported file type: {file_path.name}")

        return processed_csv_files

    # ---- Embedding Helper functions ----
    def setup_chunker_and_embedder(
        embed_model_id: str, max_tokens: int
    ) -> Tuple[SentenceTransformer, HybridChunker]:
        tokenizer = AutoTokenizer.from_pretrained(embed_model_id)
        embedding_model = SentenceTransformer(embed_model_id)
        chunker = HybridChunker(
            tokenizer=tokenizer, max_tokens=max_tokens, merge_peers=True
        )

        return embedding_model, chunker

    def embed_text(text: str, embedding_model: SentenceTransformer) -> list[float]:
        return embedding_model.encode([text], normalize_embeddings=True).tolist()[0]

    def create_chunks_with_embeddings(
        converted_data: DoclingDocument,
        embedding_model: SentenceTransformer,
        chunker: HybridChunker,
        file_name: str,
    ) -> List[Dict]:
        _log.info(f"Docling Converted data: {converted_data}")

        chunks_with_embeddings = []
        for chunk in chunker.chunk(dl_doc=converted_data):
            _log.info(f"Chunk: {chunk}")

            raw_chunk = chunker.contextualize(chunk)
            _log.info(f"Raw chunk: {raw_chunk}")

            embedding = embed_text(raw_chunk, embedding_model)
            _log.info(f"Embedding: {embedding}")

            chunk_id = str(uuid.uuid4())
            content_token_count = chunker.tokenizer.count_tokens(raw_chunk)

            # Prepare metadata object
            metadata_obj = {
                "file_name": file_name,
                "document_id": chunk_id,
                "token_count": content_token_count,
            }

            metadata_str = json.dumps(metadata_obj)
            metadata_token_count = chunker.tokenizer.count_tokens(metadata_str)
            metadata_obj["metadata_token_count"] = metadata_token_count

            # Create a new chunk with embedding
            new_chunk_with_embedding = {
                "content": raw_chunk,
                "mime_type": "text/markdown",
                "embedding": embedding,
                "metadata": metadata_obj,
            }

            _log.info(f"New embedding: {new_chunk_with_embedding}")

            chunks_with_embeddings.append(new_chunk_with_embedding)

        _log.info(f"Chunks with embeddings: {chunks_with_embeddings}")

        return chunks_with_embeddings

    def insert_chunks_with_embeddings_to_vector_db(
        chunks_with_embeddings: List[Dict],
        vector_db_id: str,
        client: LlamaStackClient,
    ) -> None:
        _log.info(
            f"Inserting chunks with embeddings to vector database: {chunks_with_embeddings}"
        )

        if chunks_with_embeddings:
            try:
                client.vector_io.insert(
                    vector_db_id=vector_db_id, chunks=chunks_with_embeddings
                )
            except Exception as e:
                _log.error(f"Failed to insert embeddings into vector database: {e}")

    def process_conversion_results(
        conv_results: Iterator[ConversionResult], client: LlamaStackClient
    ) -> None:
        processed_docs = 0
        embedding_model, chunker = setup_chunker_and_embedder(
            embed_model_id, max_tokens
        )
        for conv_res in conv_results:
            if conv_res.status != ConversionStatus.SUCCESS:
                _log.warning(
                    f"Conversion failed for {conv_res.input.file.stem}: {conv_res.status}"
                )
                continue

            processed_docs += 1
            file_name = conv_res.input.file.stem
            document = conv_res.document

            if document is None:
                _log.warning(f"Document conversion failed for {file_name}")
                continue

            chunks_with_embeddings = create_chunks_with_embeddings(
                document, embedding_model, chunker, file_name
            )

            insert_chunks_with_embeddings_to_vector_db(
                chunks_with_embeddings, vector_db_id, client
            )

        _log.info(f"Processed {processed_docs} documents successfully.")

    input_path = pathlib.Path(input_path)

    # Copy input files to the local processing dir
    input_spreadsheets_files = [input_path / name for name in spreadsheet_split]
    csv_files = convert_excel_to_csv(input_spreadsheets_files, local_processing_dir)

    _log.info(f"CSV files for Docling: {csv_files}")

    docling_csv_converter = DocumentConverter()

    # Convert all spreadsheet files to text
    conv_results = docling_csv_converter.convert_all(
        csv_files,
        raises_on_error=True,
    )

    client = LlamaStackClient(base_url=service_url)

    process_conversion_results(conv_results, client)

    # Clean up the local processing directory
    shutil.rmtree(local_processing_dir)
    _log.info(f"Cleaned up local processing directory: {local_processing_dir}")


@dsl.pipeline()
def docling_convert_pipeline(
    base_url: str = "https://raw.githubusercontent.com/opendatahub-io/rag/main/demos/testing-data/spreadsheets",
    spreadsheet_filenames: str = "people.xlsx, sample_sales_data.xlsm, test_customers.csv",
    num_workers: int = 1,
    vector_db_id: str = "csv-vector-db",
    service_url: str = "http://lsd-llama-milvus-service:8321",
    embed_model_id: str = "ibm-granite/granite-embedding-125m-english",
    max_tokens: int = 512,
    use_gpu: bool = True,  # use only if you have additional gpu worker
) -> None:
    """
    Converts spreadsheets (csv and excel) to text using Docling and generates embeddings
    :param base_url: Base URL to fetch spreadsheets
    :param spreadsheet_filenames: Comma-separated list of spreadsheets filenames to download and convert
    :param num_workers: Number of docling worker pods to use
    :param vector_db_id: ID of the vector database to store embeddings
    :param service_url: URL of the LlamaStack service
    :param embed_model_id: Model ID for embedding generation
    :param max_tokens: Maximum number of tokens per chunk
    :param use_gpu: boolean to enable/disable gpu in the docling workers
    :return:
    """
    register_task = register_vector_db(
        service_url=service_url,
        vector_db_id=vector_db_id,
        embed_model_id=embed_model_id,
    )
    register_task.set_caching_options(False)

    import_task = import_spreadsheet_files(
        base_url=base_url,
        spreadsheet_filenames=spreadsheet_filenames,
    )
    import_task.set_caching_options(True)

    spreadsheet_splits = create_spreadsheet_splits(
        input_path=import_task.output,
        num_splits=num_workers,
    ).set_caching_options(True)

    with dsl.ParallelFor(spreadsheet_splits.output) as spreadsheet_split:
        with dsl.If(use_gpu == True):
            convert_task = docling_convert_and_ingest_spreadsheets(
                input_path=import_task.output,
                spreadsheet_split=spreadsheet_split,
                embed_model_id=embed_model_id,
                max_tokens=max_tokens,
                service_url=service_url,
                vector_db_id=vector_db_id,
            )
            convert_task.set_caching_options(False)
            convert_task.set_cpu_request("500m")
            convert_task.set_cpu_limit("4")
            convert_task.set_memory_request("2Gi")
            convert_task.set_memory_limit("6Gi")
            convert_task.set_accelerator_type("nvidia.com/gpu")
            convert_task.set_accelerator_limit(1)
            add_toleration_json(
                convert_task,
                [
                    {
                        "effect": "NoSchedule",
                        "key": "nvidia.com/gpu",
                        "operator": "Exists",
                    }
                ],
            )
            add_node_selector_json(convert_task, {})
        with dsl.Else():
            convert_task = docling_convert_and_ingest_spreadsheets(
                input_path=import_task.output,
                spreadsheet_split=spreadsheet_split,
                embed_model_id=embed_model_id,
                max_tokens=max_tokens,
                service_url=service_url,
                vector_db_id=vector_db_id,
            )
            convert_task.set_caching_options(False)
            convert_task.set_cpu_request("500m")
            convert_task.set_cpu_limit("4")
            convert_task.set_memory_request("2Gi")
            convert_task.set_memory_limit("6Gi")


if __name__ == "__main__":
    compiler.Compiler().compile(
        pipeline_func=docling_convert_pipeline,
        package_path=__file__.replace(".py", "_compiled.yaml"),
    )
