import hashlib
import typing as t
from dataclasses import dataclass

from unstructured_ingest.interfaces import BaseSourceConnector
from unstructured_ingest.logger import logger
from unstructured_ingest.runner.base_runner import Runner
from unstructured_ingest.runner.utils import update_download_dir_hash

if t.TYPE_CHECKING:
    from unstructured_ingest.connector.astra import SimpleAstraConfig


@dataclass
class AstraRunner(Runner):
    connector_config: "SimpleAstraConfig"

    def update_read_config(self):
        hashed_dir_name = hashlib.sha256(
            str(self.connector_config.access_config.api_endpoint).encode("utf-8"),
        )
        self.read_config.download_dir = update_download_dir_hash(
            connector_name="astra",
            read_config=self.read_config,
            hashed_dir_name=hashed_dir_name,
            logger=logger,
        )

    def get_source_connector_cls(self) -> t.Type[BaseSourceConnector]:
        from unstructured_ingest.connector.astra import (
            AstraSourceConnector,
        )

        return AstraSourceConnector
