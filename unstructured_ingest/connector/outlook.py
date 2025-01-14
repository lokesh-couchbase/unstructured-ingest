import hashlib
import os
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from unstructured_ingest.enhanced_dataclass import enhanced_field
from unstructured_ingest.error import SourceConnectionError, SourceConnectionNetworkError
from unstructured_ingest.interfaces import (
    AccessConfig,
    BaseConnectorConfig,
    BaseSingleIngestDoc,
    BaseSourceConnector,
    IngestDocCleanupMixin,
    SourceConnectorCleanupMixin,
    SourceMetadata,
)
from unstructured_ingest.logger import logger
from unstructured_ingest.utils.dep_check import requires_dependencies

MAX_NUM_EMAILS = 1000000  # Maximum number of emails per folder
if t.TYPE_CHECKING:
    from office365.graph_client import GraphClient


class MissingFolderError(Exception):
    """There are no root folders with those names."""


@dataclass
class OutlookAccessConfig(AccessConfig):
    client_credential: str = enhanced_field(repr=False, sensitive=True, overload_name="client_cred")


@dataclass
class SimpleOutlookConfig(BaseConnectorConfig):
    """This class is getting the token."""

    access_config: OutlookAccessConfig
    user_email: str
    client_id: str
    tenant: t.Optional[str] = field(repr=False, default="common")
    authority_url: t.Optional[str] = field(repr=False, default="https://login.microsoftonline.com")
    outlook_folders: t.List[str] = field(default_factory=list)
    recursive: bool = False
    registry_name: str = "outlook"

    def __post_init__(self):
        if not (self.client_id and self.access_config.client_credential and self.user_email):
            raise ValueError(
                "Please provide one of the following mandatory values:"
                "\nclient_id\nclient_cred\nuser_email",
            )
        self.token_factory = self._acquire_token

    @requires_dependencies(["msal"])
    def _acquire_token(self):
        from msal import ConfidentialClientApplication

        try:
            app = ConfidentialClientApplication(
                authority=f"{self.authority_url}/{self.tenant}",
                client_id=self.client_id,
                client_credential=self.access_config.client_credential,
            )
            token = app.acquire_token_for_client(
                scopes=["https://graph.microsoft.com/.default"],
            )
        except ValueError as exc:
            logger.error("Couldn't set up credentials for Outlook")
            raise exc
        return token

    @requires_dependencies(["office365"], extras="outlook")
    def _get_client(self):
        from office365.graph_client import GraphClient

        return GraphClient(self.token_factory)


@dataclass
class OutlookIngestDoc(IngestDocCleanupMixin, BaseSingleIngestDoc):
    connector_config: SimpleOutlookConfig
    message_id: str
    registry_name: str = "outlook"

    def __post_init__(self):
        self._set_download_paths()

    def hash_mail_name(self, id):
        """Outlook email ids are 152 char long. Hash to shorten to 16."""
        return hashlib.sha256(id.encode("utf-8")).hexdigest()[:16]

    def _set_download_paths(self) -> None:
        """Creates paths for downloading and parsing."""
        download_path = Path(f"{self.read_config.download_dir}")
        output_path = Path(f"{self.processor_config.output_dir}")

        self.download_dir = download_path
        self.download_filepath = (
            download_path / f"{self.hash_mail_name(self.message_id)}.eml"
        ).resolve()
        oname = f"{self.hash_mail_name(self.message_id)}.eml.json"
        self.output_dir = output_path
        self.output_filepath = (output_path / oname).resolve()

    @property
    def filename(self):
        return Path(self.download_filepath).resolve()

    @property
    def _output_filename(self):
        return Path(self.output_filepath).resolve()

    @property
    def record_locator(self) -> t.Optional[t.Dict[str, t.Any]]:
        return {
            "message_id": self.message_id,
            "user_email": self.connector_config.user_email,
        }

    @requires_dependencies(["office365"], extras="outlook")
    def update_source_metadata(self, **kwargs):
        from office365.runtime.client_request_exception import ClientRequestException

        try:
            client = self.connector_config._get_client()
            msg = (
                client.users[self.connector_config.user_email]
                .messages[self.message_id]
                .get()
                .execute_query()
            )
        except ClientRequestException as e:
            if e.response.status_code == 404:
                self.source_metadata = SourceMetadata(
                    exists=False,
                )
                return
            raise
        self.source_metadata = SourceMetadata(
            date_created=msg.created_datetime.isoformat(),
            date_modified=msg.last_modified_datetime.isoformat(),
            version=msg.get_property("changeKey"),
            source_url=msg.get_property("webLink"),
            exists=True,
        )

    @SourceConnectionNetworkError.wrap
    def _run_download(self, local_file):
        client = self.connector_config._get_client()
        client.users[self.connector_config.user_email].messages[self.message_id].download(
            local_file,
        ).execute_query()

    @SourceConnectionError.wrap
    @BaseSingleIngestDoc.skip_if_file_exists
    @requires_dependencies(["office365"], extras="outlook")
    def get_file(self):
        """Relies on Office365 python sdk message object to do the download."""
        try:
            self.connector_config._get_client()
            self.update_source_metadata()
            if not self.download_dir.is_dir():
                logger.debug(f"Creating directory: {self.download_dir}")
                self.download_dir.mkdir(parents=True, exist_ok=True)

            with open(
                os.path.join(
                    self.download_dir,
                    self.hash_mail_name(self.message_id) + ".eml",
                ),
                "wb",
            ) as local_file:
                self._run_download(local_file=local_file)

        except Exception as e:
            logger.error(
                f"Error while downloading and saving file: {self.hash_mail_name(self.message_id)}.",
            )
            logger.error(e)
            return
        logger.info(f"File downloaded: {self.hash_mail_name(self.message_id)}")
        return


@dataclass
class OutlookSourceConnector(SourceConnectorCleanupMixin, BaseSourceConnector):
    connector_config: SimpleOutlookConfig
    _client: t.Optional["GraphClient"] = field(init=False, default=None)

    @property
    def client(self) -> "GraphClient":
        if self._client is None:
            self._client = self.connector_config._get_client()
        return self._client

    def initialize(self):
        try:
            self.get_folder_ids()
        except Exception as e:
            raise SourceConnectionError(f"failed to validate connection: {e}")

    def check_connection(self):
        try:
            _ = self.client
        except Exception as e:
            logger.error(f"failed to validate connection: {e}", exc_info=True)
            raise SourceConnectionError(f"failed to validate connection: {e}")

    def recurse_folders(self, folder_id, main_folder_dict):
        """We only get a count of subfolders for any folder.
        Have to make additional calls to get subfolder ids."""
        subfolders = (
            self.client.users[self.connector_config.user_email]
            .mail_folders[folder_id]
            .child_folders.get()
            .execute_query()
        )
        for subfolder in subfolders:
            for k, v in main_folder_dict.items():
                if subfolder.get_property("parentFolderId") in v:
                    v.append(subfolder.id)
            if subfolder.get_property("childFolderCount") > 0:
                self.recurse_folders(subfolder.id, main_folder_dict)

    def get_folder_ids(self):
        """Sets the mail folder ids and subfolder ids for requested root mail folders."""
        self.root_folders = defaultdict(list)
        root_folders_with_subfolders = []
        get_root_folders = (
            self.client.users[self.connector_config.user_email].mail_folders.get().execute_query()
        )

        for folder in get_root_folders:
            self.root_folders[folder.display_name].append(folder.id)
            if folder.get_property("childFolderCount") > 0:
                root_folders_with_subfolders.append(folder.id)

        for folder in root_folders_with_subfolders:
            self.recurse_folders(folder, self.root_folders)

        # Narrow down all mail folder ids (plus all subfolders) to the ones that were requested.
        self.selected_folder_ids = list(
            chain.from_iterable(
                [
                    v
                    for k, v in self.root_folders.items()
                    if k.lower() in [x.lower() for x in self.connector_config.outlook_folders]
                ],
            ),
        )
        if not self.selected_folder_ids:
            raise MissingFolderError(
                "There are no root folders with the names: "
                f"{self.connector_config.outlook_folders}",
            )

    def get_ingest_docs(self):
        """Returns a list of all the message objects that are in the requested root folder(s)."""
        filtered_messages = []

        # Get all the relevant messages in the selected folders/subfolders.
        for folder_id in self.selected_folder_ids:
            messages = (
                self.client.users[self.connector_config.user_email]
                .mail_folders[folder_id]
                .messages.get()
                .top(MAX_NUM_EMAILS)  # Prevents the return from paging
                .execute_query()
            )
            # Skip empty list if there are no messages in folder.
            if messages:
                filtered_messages.append(messages)
        return [
            OutlookIngestDoc(
                connector_config=self.connector_config,
                processor_config=self.processor_config,
                read_config=self.read_config,
                message_id=message.id,
            )
            for message in list(chain.from_iterable(filtered_messages))
        ]
