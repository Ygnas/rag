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
    packages_to_install=["llama-stack-client", "fire", "httpx"],
)
def clear_vector_db(
    service_url: str,
    vector_db_id: str,
):
    """Unregisters (deletes) a vector database if it exists."""
    from llama_stack_client import LlamaStackClient

    client = LlamaStackClient(base_url=service_url)

    try:
        print(f"Attempting to clear vector DB '{vector_db_id}'...")
        client.vector_dbs.unregister(vector_db_id=vector_db_id)
        print(f"Successfully cleared vector DB '{vector_db_id}'.")

    except Exception as e:
        print(
            f"Warning: Could not clear vector DB '{vector_db_id}'."
            f"This is expected if it's the first run. Error: {e}"
        )


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
def import_audio_files(
    base_url: str,
    audio_filenames: str,
    output_path: dsl.OutputPath("input-recordings"),
):
    import os
    import requests
    import shutil

    os.makedirs(output_path, exist_ok=True)
    filenames = [f.strip() for f in audio_filenames.split(",") if f.strip()]

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
def create_audio_splits(
    input_path: dsl.InputPath("input-recordings"),
    num_splits: int,
) -> List[List[str]]:
    import pathlib

    # Split our entire directory of audio files into n batches, where n == num_splits
    # Support common audio formats
    audio_extensions = ["*.wav", "*.mp3", "*.m4a", "*.flac", "*.ogg", "*.aac"]
    all_audio = []

    input_dir = pathlib.Path(input_path)
    for ext in audio_extensions:
        all_audio.extend([path.name for path in input_dir.glob(ext)])

    splits = [
        batch
        for batch in (all_audio[i::num_splits] for i in range(num_splits))
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
        "openai-whisper",
        "torch",
        "torchaudio",
    ],
)
def docling_convert_and_ingest_audio(
    input_path: dsl.InputPath("input-recordings"),
    audio_split: List[str],
    output_path: dsl.OutputPath("output-md"),
    embed_model_id: str,
    max_tokens: int,
    service_url: str,
    vector_db_id: str,
):
    import pathlib
    import subprocess
    import os
    import re

    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.document import ConversionResult
    from docling.datamodel.pipeline_options import AsrPipelineOptions
    from docling.datamodel import asr_model_specs
    from docling.document_converter import AudioFormatOption, DocumentConverter
    from docling.pipeline.asr_pipeline import AsrPipeline
    from docling_core.types.doc.document import DoclingDocument

    from transformers import AutoTokenizer
    from sentence_transformers import SentenceTransformer
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    import logging
    from llama_stack_client import LlamaStackClient
    import uuid
    import json

    _log = logging.getLogger(__name__)

    # Install ffmpeg from https://ffmpeg.org/download.html#build-linux
    def install_ffmpeg() -> None:
        try:
            # Check if ffmpeg is already installed
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
            print("ffmpeg is already installed")
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Installing ffmpeg...")

        try:
            print("Package management restricted, downloading static ffmpeg binary...")

            import urllib.request
            import stat
            import pathlib
            import shutil

            # Create temp directory
            temp_dir = pathlib.Path("/tmp/ffmpeg_install")
            temp_dir.mkdir(exist_ok=True)

            # Download static ffmpeg binary
            ffmpeg_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
            ffmpeg_archive = temp_dir / "ffmpeg-static.tar.xz"

            print(f"Downloading ffmpeg from {ffmpeg_url}")
            urllib.request.urlretrieve(ffmpeg_url, ffmpeg_archive)

            print("Extracting ffmpeg archive...")
            subprocess.run(
                ["tar", "-xf", str(ffmpeg_archive), "-C", str(temp_dir)],
                check=True,
                capture_output=True,
            )

            ffmpeg_path_candidates = list(temp_dir.rglob("ffmpeg"))
            if not ffmpeg_path_candidates:
                raise FileNotFoundError(
                    "Could not find 'ffmpeg' executable in the extracted archive."
                )

            ffmpeg_path = ffmpeg_path_candidates[0]

            # Make executable
            ffmpeg_path.chmod(
                ffmpeg_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

            # Use a writable bin directory and add it to the PATH
            bin_dir = pathlib.Path("/tmp/bin")
            bin_dir.mkdir(exist_ok=True)
            target_path = bin_dir / "ffmpeg"

            # Move the file to the target path
            ffmpeg_path.rename(target_path)

            # Add to PATH environment variable
            os.environ["PATH"] = f"{str(bin_dir)}:{os.environ.get('PATH', '')}"

            # Verify installation
            subprocess.run(
                [str(target_path), "-version"], capture_output=True, check=True
            )
            print(f"Static ffmpeg binary installed to {target_path} and added to PATH")

            # Clean up extraction directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            return

        except Exception as e:
            print(f"Failed to install ffmpeg: {e}")
            raise RuntimeError(
                "ffmpeg installation failed. Audio processing requires ffmpeg."
            ) from e

    # Convert audio files to WAV format that whisper can process
    def convert_audio_to_wav(
        input_audio_files: List[pathlib.Path],
    ) -> Tuple[List[pathlib.Path], List[pathlib.Path]]:
        processed_audio_files = []
        temp_files_to_cleanup = []

        for audio_file in input_audio_files:
            if not audio_file.exists():
                print(f"Skipping missing file: {audio_file}")
                continue

            # Check if file is already WAV
            if audio_file.suffix.lower() == ".wav":
                processed_audio_files.append(audio_file)
                print(f"Using WAV file directly: {audio_file.name}")
                continue

            # Convert non-WAV files to WAV format using ffmpeg
            print(f"Converting {audio_file.name} to WAV format...")
            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix=f"_{audio_file.stem}.wav", delete=False
            ) as tmp:
                temp_wav = pathlib.Path(tmp.name)

            try:
                # Use ffmpeg to convert to WAV format
                subprocess.run(
                    [
                        "ffmpeg",
                        "-i",
                        str(audio_file),
                        "-ar",
                        "16000",  # 16kHz sample rate (good for whisper)
                        "-ac",
                        "1",  # mono channel
                        "-c:a",
                        "pcm_s16le",  # 16-bit PCM
                        "-y",  # overwrite output file
                        str(temp_wav),
                    ],
                    check=True,
                    capture_output=True,
                )

                processed_audio_files.append(temp_wav)
                temp_files_to_cleanup.append(temp_wav)
                print(f"Successfully converted {audio_file.name} to WAV format")

            except subprocess.CalledProcessError as e:
                print(f"ffmpeg conversion failed for {audio_file.name}: {e}")
                if e.stderr:
                    print(f"stderr: {e.stderr.decode()}")
                continue
        return (processed_audio_files, temp_files_to_cleanup)

    # Clean up temporary files
    def cleanup_temp_files(temp_files_to_cleanup: List[pathlib.Path]) -> None:
        for temp_file in temp_files_to_cleanup:
            temp_file.unlink(missing_ok=True)
            print(f"Cleaned up temporary file: {temp_file.name}")

    def clean_timestamps(doc: DoclingDocument) -> None:
        for item in doc.texts:
            cleaned_text = re.sub(r"\[time: .*?\]\s*", "", item.text)
            item.text = cleaned_text
            item.orig = cleaned_text

    # Return a Docling DocumentConverter configured for ASR with whisper_turbo model.
    def get_asr_converter() -> DocumentConverter:
        """Create a DocumentConverter configured for ASR with whisper_turbo model."""
        pipeline_options = AsrPipelineOptions()
        pipeline_options.asr_options = asr_model_specs.WHISPER_TURBO
        pipeline_options.asr_options.timestamps = False
        pipeline_options.asr_options.word_timestamps = False
        pipeline_options.asr_options.verbose = False

        converter = DocumentConverter(
            format_options={
                InputFormat.AUDIO: AudioFormatOption(
                    pipeline_cls=AsrPipeline,
                    pipeline_options=pipeline_options,
                )
            }
        )

        return converter

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
        chunks_with_embeddings = []
        for chunk in chunker.chunk(dl_doc=converted_data):
            raw_chunk = chunker.contextualize(chunk)
            embedding = embed_text(raw_chunk, embedding_model)
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

            print(f"New embedding: {new_chunk_with_embedding}")

            chunks_with_embeddings.append(new_chunk_with_embedding)

        return chunks_with_embeddings

    def insert_chunks_with_embeddings_to_vector_db(
        chunks_with_embeddings: List[Dict],
        vector_db_id: str,
        client: LlamaStackClient,
    ) -> None:
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
            clean_timestamps(document)

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

    # Install ffmpeg before proceeding
    install_ffmpeg()

    input_path = pathlib.Path(input_path)
    output_path = pathlib.Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    input_audio_files = [input_path / name for name in audio_split]

    processed_audio_files, temp_files_to_cleanup = convert_audio_to_wav(
        input_audio_files
    )

    # Create Docling ASR converter
    docling_asr_converter = get_asr_converter()

    # Convert all audio files to text
    conv_results = docling_asr_converter.convert_all(
        processed_audio_files,
        raises_on_error=True,
    )

    client = LlamaStackClient(base_url=service_url)

    process_conversion_results(conv_results, client)

    cleanup_temp_files(temp_files_to_cleanup)


@dsl.pipeline()
def docling_convert_pipeline(
    base_url: str = "https://raw.githubusercontent.com/opendatahub-io/rag/main/demos/testing-data/audio-speech",
    audio_filenames: str = "RAG_use_cases.wav, RAG_customers.wav, RAG_benefits.m4a, RAG_vs_Regular_LLM_Output.m4a",
    num_workers: int = 1,
    vector_db_id: str = "asr-vector-db",
    service_url: str = "http://lsd-llama-milvus-service:8321",
    embed_model_id: str = "ibm-granite/granite-embedding-125m-english",
    max_tokens: int = 512,
    use_gpu: bool = True,  # use only if you have additional gpu worker
    clean_vector_db: bool = False,  # if True, the vector database will be cleared during running the pipeline
) -> None:
    """
    Converts audio recordings to text using Docling ASR and generates embeddings
    :param base_url: Base URL to fetch audio files
    :param audio_filenames: Comma-separated list of audio filenames to download and convert
    :param num_workers: Number of docling worker pods to use
    :param vector_db_id: ID of the vector database to store embeddings
    :param service_url: URL of the LlamaStack service
    :param embed_model_id: Model ID for embedding generation
    :param max_tokens: Maximum number of tokens per chunk
    :param use_gpu: boolean to enable/disable gpu in the docling workers
    :param clean_vector_db: boolean to enable/disable clearing the vector database before running the pipeline
    :return:
    """
    with dsl.If(clean_vector_db == True):
        clear_task = clear_vector_db(
            service_url=service_url,
            vector_db_id=vector_db_id,
        )
        clear_task.set_caching_options(False)

        register_task = register_vector_db(
            service_url=service_url,
            vector_db_id=vector_db_id,
            embed_model_id=embed_model_id,
        ).after(clear_task)
        register_task.set_caching_options(False)

        import_task = import_audio_files(
            base_url=base_url,
            audio_filenames=audio_filenames,
        )
        import_task.set_caching_options(True)

        audio_splits = create_audio_splits(
            input_path=import_task.output,
            num_splits=num_workers,
        ).set_caching_options(True)

        with dsl.ParallelFor(audio_splits.output) as audio_split:
            with dsl.If(use_gpu == True):
                convert_task = docling_convert_and_ingest_audio(
                    input_path=import_task.output,
                    audio_split=audio_split,
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
                convert_task = docling_convert_and_ingest_audio(
                    input_path=import_task.output,
                    audio_split=audio_split,
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

    with dsl.Else():
        register_task = register_vector_db(
            service_url=service_url,
            vector_db_id=vector_db_id,
            embed_model_id=embed_model_id,
        )
        register_task.set_caching_options(False)

        import_task = import_audio_files(
            base_url=base_url,
            audio_filenames=audio_filenames,
        )
        import_task.set_caching_options(True)

        audio_splits = create_audio_splits(
            input_path=import_task.output,
            num_splits=num_workers,
        ).set_caching_options(True)

        with dsl.ParallelFor(audio_splits.output) as audio_split:
            with dsl.If(use_gpu == True):
                convert_task = docling_convert_and_ingest_audio(
                    input_path=import_task.output,
                    audio_split=audio_split,
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
                convert_task = docling_convert_and_ingest_audio(
                    input_path=import_task.output,
                    audio_split=audio_split,
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
