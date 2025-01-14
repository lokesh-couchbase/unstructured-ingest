import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from dateutil import parser

from unstructured_ingest.enhanced_dataclass import enhanced_field
from unstructured_ingest.error import DestinationConnectionError
from unstructured_ingest.utils.data_prep import batch_generator, flatten_dict
from unstructured_ingest.utils.dep_check import requires_dependencies
from unstructured_ingest.v2.interfaces import (
    AccessConfig,
    ConnectionConfig,
    FileData,
    UploadContent,
    Uploader,
    UploaderConfig,
    UploadStager,
    UploadStagerConfig,
)
from unstructured_ingest.v2.logger import logger
from unstructured_ingest.v2.processes.connector_registry import (
    DestinationRegistryEntry,
)

if TYPE_CHECKING:
    from chromadb import Client

CONNECTOR_TYPE = "chroma"


@dataclass
class ChromaAccessConfig(AccessConfig):
    settings: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None


@dataclass
class ChromaConnectionConfig(ConnectionConfig):
    collection_name: str
    access_config: ChromaAccessConfig = enhanced_field(sensitive=True)
    path: Optional[str] = None
    tenant: Optional[str] = "default_tenant"
    database: Optional[str] = "default_database"
    host: Optional[str] = None
    port: Optional[int] = None
    ssl: bool = False
    connector_type: str = CONNECTOR_TYPE


@dataclass
class ChromaUploadStagerConfig(UploadStagerConfig):
    pass


@dataclass
class ChromaUploadStager(UploadStager):
    upload_stager_config: ChromaUploadStagerConfig = field(
        default_factory=lambda: ChromaUploadStagerConfig()
    )

    @staticmethod
    def parse_date_string(date_string: str) -> date:
        try:
            timestamp = float(date_string)
            return datetime.fromtimestamp(timestamp)
        except Exception as e:
            logger.debug(f"date {date_string} string not a timestamp: {e}")
        return parser.parse(date_string)

    @staticmethod
    def conform_dict(data: dict) -> dict:
        """
        Prepares dictionary in the format that Chroma requires
        """
        element_id = data.get("element_id", str(uuid.uuid4()))
        return {
            "id": element_id,
            "embedding": data.pop("embeddings", None),
            "document": data.pop("text", None),
            "metadata": flatten_dict(data, separator="-", flatten_lists=True, remove_none=True),
        }

    def run(
        self,
        elements_filepath: Path,
        file_data: FileData,
        output_dir: Path,
        output_filename: str,
        **kwargs: Any,
    ) -> Path:
        with open(elements_filepath) as elements_file:
            elements_contents = json.load(elements_file)
        conformed_elements = [self.conform_dict(data=element) for element in elements_contents]
        output_path = Path(output_dir) / Path(f"{output_filename}.json")
        with open(output_path, "w") as output_file:
            json.dump(conformed_elements, output_file)
        return output_path


@dataclass
class ChromaUploaderConfig(UploaderConfig):
    batch_size: int = 100


@dataclass
class ChromaUploader(Uploader):
    connector_type: str = CONNECTOR_TYPE
    upload_config: ChromaUploaderConfig
    connection_config: ChromaConnectionConfig

    def precheck(self) -> None:
        try:
            self.create_client()
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise DestinationConnectionError(f"failed to validate connection: {e}")

    @requires_dependencies(["chromadb"], extras="chroma")
    def create_client(self) -> "Client":
        import chromadb

        if self.connection_config.path:
            return chromadb.PersistentClient(
                path=self.connection_config.path,
                settings=self.connection_config.access_config.settings,
                tenant=self.connection_config.tenant,
                database=self.connection_config.database,
            )

        elif self.connection_config.host and self.connection_config.port:
            return chromadb.HttpClient(
                host=self.connection_config.host,
                port=self.connection_config.port,
                ssl=self.connection_config.ssl,
                headers=self.connection_config.access_config.headers,
                settings=self.connection_config.access_config.settings,
                tenant=self.connection_config.tenant,
                database=self.connection_config.database,
            )
        else:
            raise ValueError("Chroma connector requires either path or host and port to be set.")

    @DestinationConnectionError.wrap
    def upsert_batch(self, collection, batch):

        try:
            # Chroma wants lists even if there is only one element
            # Upserting to prevent duplicates
            collection.upsert(
                ids=batch["ids"],
                documents=batch["documents"],
                embeddings=batch["embeddings"],
                metadatas=batch["metadatas"],
            )
        except Exception as e:
            raise ValueError(f"chroma error: {e}") from e

    @staticmethod
    def prepare_chroma_list(chunk: tuple[dict[str, Any]]) -> dict[str, list[Any]]:
        """Helper function to break a tuple of dicts into list of parallel lists for ChromaDb.
        ({'id':1}, {'id':2}, {'id':3}) -> {'ids':[1,2,3]}"""
        chroma_dict = {}
        chroma_dict["ids"] = [x.get("id") for x in chunk]
        chroma_dict["documents"] = [x.get("document") for x in chunk]
        chroma_dict["embeddings"] = [x.get("embedding") for x in chunk]
        chroma_dict["metadatas"] = [x.get("metadata") for x in chunk]
        # Make sure all lists are of the same length
        assert (
            len(chroma_dict["ids"])
            == len(chroma_dict["documents"])
            == len(chroma_dict["embeddings"])
            == len(chroma_dict["metadatas"])
        )
        return chroma_dict

    def run(self, contents: list[UploadContent], **kwargs: Any) -> None:

        elements_dict = []
        for content in contents:
            with open(content.path) as elements_file:
                elements = json.load(elements_file)
                elements_dict.extend(elements)

        logger.info(
            f"writing {len(elements_dict)} objects to destination "
            f"collection {self.connection_config.collection_name} "
            f"at {self.connection_config.host}",
        )
        client = self.create_client()

        collection = client.get_or_create_collection(name=self.connection_config.collection_name)
        for chunk in batch_generator(elements_dict, self.upload_config.batch_size):
            self.upsert_batch(collection, self.prepare_chroma_list(chunk))


chroma_destination_entry = DestinationRegistryEntry(
    connection_config=ChromaConnectionConfig,
    uploader=ChromaUploader,
    uploader_config=ChromaUploaderConfig,
    upload_stager=ChromaUploadStager,
    upload_stager_config=ChromaUploadStagerConfig,
)
