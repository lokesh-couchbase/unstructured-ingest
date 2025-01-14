import os
from pathlib import Path

from unstructured_ingest.v2.interfaces import ProcessorConfig
from unstructured_ingest.v2.logger import logger
from unstructured_ingest.v2.pipeline.pipeline import Pipeline
from unstructured_ingest.v2.processes.chunker import ChunkerConfig
from unstructured_ingest.v2.processes.connectors.elasticsearch import (
    ElasticsearchAccessConfig,
    ElasticsearchConnectionConfig,
    ElasticsearchUploaderConfig,
    ElasticsearchUploadStagerConfig,
)
from unstructured_ingest.v2.processes.connectors.local import (
    LocalConnectionConfig,
    LocalDownloaderConfig,
    LocalIndexerConfig,
)
from unstructured_ingest.v2.processes.embedder import EmbedderConfig
from unstructured_ingest.v2.processes.partitioner import PartitionerConfig

base_path = Path(__file__).parent.parent.parent.parent.parent
docs_path = base_path / "example-docs"
work_dir = base_path / "tmp_ingest"
output_path = work_dir / "output"
download_path = work_dir / "download"

if __name__ == "__main__":
    logger.info(f"Writing all content in: {work_dir.resolve()}")
    index_name = "ingest-test-destination"
    Pipeline.from_configs(
        context=ProcessorConfig(work_dir=str(work_dir.resolve())),
        indexer_config=LocalIndexerConfig(
            input_path=str(docs_path.resolve()) + "/book-war-and-peace-1p.txt"
        ),
        downloader_config=LocalDownloaderConfig(download_dir=download_path),
        source_connection_config=LocalConnectionConfig(),
        partitioner_config=PartitionerConfig(strategy="fast"),
        chunker_config=ChunkerConfig(chunking_strategy="by_title"),
        embedder_config=EmbedderConfig(embedding_provider="langchain-huggingface"),
        destination_connection_config=ElasticsearchConnectionConfig(
            access_config=ElasticsearchAccessConfig(password=os.getenv("ELASTIC_PASSWORD")),
            username=os.getenv("ELASTIC_USERNAME"),
            hosts=["http://localhost:9200"],
        ),
        uploader_config=ElasticsearchUploaderConfig(index_name=index_name),
        stager_config=ElasticsearchUploadStagerConfig(index_name=index_name),
    ).run()
