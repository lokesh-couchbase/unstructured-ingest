import copy
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

from unstructured.__version__ import __version__ as unstructured_version

from unstructured_ingest.enhanced_dataclass import enhanced_field
from unstructured_ingest.enhanced_dataclass.core import _asdict
from unstructured_ingest.error import DestinationConnectionError, SourceConnectionError, WriteError
from unstructured_ingest.interfaces import (
    AccessConfig,
    BaseConnectorConfig,
    BaseDestinationConnector,
    BaseIngestDocBatch,
    BaseSingleIngestDoc,
    BaseSourceConnector,
    IngestDocCleanupMixin,
    SourceConnectorCleanupMixin,
    SourceMetadata,
)
from unstructured_ingest.logger import logger
from unstructured_ingest.utils.data_prep import flatten_dict
from unstructured_ingest.utils.dep_check import requires_dependencies

if t.TYPE_CHECKING:
    from pymongo import MongoClient


SERVER_API_VERSION = "1"


def parse_userinfo(userinfo: str) -> t.Tuple[str, str]:
    user, _, passwd = userinfo.partition(":")
    return user, passwd


@dataclass
class MongoDBAccessConfig(AccessConfig):
    uri: t.Optional[str] = enhanced_field(sensitive=True, default=None)


@dataclass
class SimpleMongoDBConfig(BaseConnectorConfig):
    access_config: MongoDBAccessConfig
    host: t.Optional[str] = None
    database: t.Optional[str] = None
    collection: t.Optional[str] = None
    port: int = 27017
    batch_size: int = 100

    @requires_dependencies(["pymongo"], extras="mongodb")
    def generate_client(self) -> "MongoClient":
        from pymongo import MongoClient
        from pymongo.driver_info import DriverInfo
        from pymongo.server_api import ServerApi

        if self.access_config.uri:
            return MongoClient(
                self.access_config.uri,
                server_api=ServerApi(version=SERVER_API_VERSION),
                driver=DriverInfo(name="unstructured", version=unstructured_version),
            )
        else:
            return MongoClient(
                host=self.host,
                port=self.port,
                server_api=ServerApi(version=SERVER_API_VERSION),
            )

    def get_collection(self, client):
        database = client[self.database]
        return database.get_collection(name=self.collection)


@dataclass
class MongoDBDocumentMeta:
    collection: str
    document_id: str
    date_created: str


@dataclass
class MongoDBIngestDoc(IngestDocCleanupMixin, BaseSingleIngestDoc):
    connector_config: SimpleMongoDBConfig
    document_meta: MongoDBDocumentMeta
    document: dict = field(default_factory=dict)
    registry_name: str = "mongodb"

    @property
    def filename(self):
        return (
            Path(self.read_config.download_dir)
            / self.connector_config.collection
            / f"{self.document_meta.document_id}.txt"
        ).resolve()

    @property
    def _output_filename(self):
        return (
            Path(self.processor_config.output_dir)
            / self.connector_config.collection
            / f"{self.document_meta.document_id}.json"
        )

    def update_source_metadata(self, **kwargs):
        if self.document is None:
            self.source_metadata = SourceMetadata(
                exists=False,
            )
            return
        self.source_metadata = SourceMetadata(
            date_created=self.document_meta.date_created,
            exists=True,
        )

    @SourceConnectionError.wrap
    @requires_dependencies(["pymongo"], extras="mongodb")
    @BaseSingleIngestDoc.skip_if_file_exists
    def get_file(self):
        pass

    @property
    def record_locator(self) -> t.Optional[t.Dict[str, t.Any]]:
        return {
            "host": self.connector_config.host,
            "collection": self.connector_config.collection,
            "document_id": self.document_meta.document_id,
        }


@dataclass
class MongoDBIngestDocBatch(BaseIngestDocBatch):
    connector_config: SimpleMongoDBConfig
    ingest_docs: t.List[MongoDBIngestDoc] = field(default_factory=list)
    list_of_ids: t.List[str] = field(default_factory=list)
    registry_name: str = "mongodb_batch"

    @property
    def unique_id(self) -> str:
        return ",".join(sorted(self.list_of_ids))

    @requires_dependencies(["pymongo"], extras="mongodb")
    def _get_docs(self) -> t.List[dict]:
        """Fetches all documents in a collection."""
        from bson.objectid import ObjectId

        # Note for future. Maybe this could use other client
        client = self.connector_config.generate_client()
        collection = self.connector_config.get_collection(client)
        # MondoDB expects a list of ObjectIds
        list_of_object_ids = []
        for x in self.list_of_ids:
            list_of_object_ids.append(ObjectId(x))
        return list(collection.find({"_id": {"$in": list_of_object_ids}}))

    def get_files(self):
        documents = self._get_docs()
        for doc in documents:
            ingest_doc = MongoDBIngestDoc(
                processor_config=self.processor_config,
                read_config=self.read_config,
                connector_config=self.connector_config,
                document_meta=MongoDBDocumentMeta(
                    collection=self.connector_config.collection,
                    document_id=str(doc.get("_id")),
                    date_created=doc.get("_id").generation_time.isoformat(),
                ),
                document=doc,
            )
            ingest_doc.update_source_metadata()
            del doc["_id"]
            filename = ingest_doc.filename
            flattened_dict = flatten_dict(dictionary=doc)
            str_values = [str(value) for value in flattened_dict.values()]
            concatenated_values = "\n".join(str_values)

            filename.parent.mkdir(parents=True, exist_ok=True)
            with open(filename, "w", encoding="utf8") as f:
                f.write(concatenated_values)

            self.ingest_docs.append(ingest_doc)


@dataclass
class MongoDBSourceConnector(SourceConnectorCleanupMixin, BaseSourceConnector):
    connector_config: SimpleMongoDBConfig
    _client: t.Optional["MongoClient"] = field(init=False, default=None)

    @property
    def client(self) -> "MongoClient":
        if self._client is None:
            self._client = self.connector_config.generate_client()
        return self._client

    def check_connection(self):
        try:
            self.client.admin.command("ping")
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise DestinationConnectionError(f"failed to validate connection: {e}")

    def initialize(self):
        _ = self.client

    @requires_dependencies(["pymongo"], extras="mongodb")
    def _get_doc_ids(self) -> t.List[str]:
        """Fetches all document ids in a collection."""
        collection = self.connector_config.get_collection(self.client)
        return [str(x) for x in collection.distinct("_id")]

    def get_ingest_docs(self):
        """Fetches all documents in an index, using ids that are fetched with _get_doc_ids"""
        ids = self._get_doc_ids()
        id_batches = [
            ids[
                i
                * self.connector_config.batch_size : (i + 1)  # noqa
                * self.connector_config.batch_size
            ]
            for i in range(
                (len(ids) + self.connector_config.batch_size - 1)
                // self.connector_config.batch_size
            )
        ]

        return [
            MongoDBIngestDocBatch(
                connector_config=self.connector_config,
                processor_config=self.processor_config,
                read_config=self.read_config,
                list_of_ids=batched_ids,
            )
            for batched_ids in id_batches
        ]


@dataclass
class MongoDBDestinationConnector(BaseDestinationConnector):
    connector_config: SimpleMongoDBConfig
    _client: t.Optional["MongoClient"] = field(init=False, default=None)

    def to_dict(self, **kwargs):
        """
        The _client variable in this dataclass breaks deepcopy due to:
        TypeError: cannot pickle '_thread.lock' object
        When serializing, remove it, meaning client data will need to be reinitialized
        when deserialized
        """
        self_cp = copy.copy(self)
        if hasattr(self_cp, "_client"):
            setattr(self_cp, "_client", None)
        return _asdict(self_cp, **kwargs)

    @property
    def client(self) -> "MongoClient":
        if self._client is None:
            self._client = self.connector_config.generate_client()
        return self._client

    @requires_dependencies(["pymongo"], extras="mongodb")
    def check_connection(self):
        try:
            self.client.admin.command("ping")
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise DestinationConnectionError(f"failed to validate connection: {e}")

    def initialize(self):
        _ = self.client

    @requires_dependencies(["pymongo"], extras="mongodb")
    def write_dict(self, *args, elements_dict: t.List[t.Dict[str, t.Any]], **kwargs) -> None:
        logger.info(
            f"writing {len(elements_dict)} documents to destination "
            f"database {self.connector_config.database}, "
            f"at collection {self.connector_config.collection}",
        )

        collection = self.connector_config.get_collection(self.client)
        try:
            collection.insert_many(elements_dict)
        except Exception as e:
            logger.error(f"failed to write records: {e}", exc_info=True)
            raise WriteError(f"failed to write records: {e}")
