import os
import posixpath
import re
import urllib.parse

from mlflow.entities import FileInfo
from mlflow.environment_variables import MLFLOW_ARTIFACT_UPLOAD_DOWNLOAD_TIMEOUT
from mlflow.exceptions import MlflowException
from mlflow.store.artifact.artifact_repo import ArtifactRepository


def _parse_abfss_uri(uri):
    """
    Parse an ABFSS URI in the format
    "abfss://<file_system>@<account_name>.dbfs.core.windows.net/<path>",
    returning a tuple consisting of the filesystem, account name, and path

    See more details about ABFSS URIs at
    https://learn.microsoft.com/en-us/azure/storage/blobs/data-lake-storage-abfs-driver#uri-scheme-to-reference-data

    :param uri: ABFSS URI to parse
    :return: A tuple containing the name of the filesystem, account name, and path
    """
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "abfss":
        raise MlflowException(f"Not an ABFSS URI: {uri}")

    match = re.match(r"([^@]+)@([^.]+)\.dfs\.core\.windows\.net", parsed.netloc)

    if match is None:
        raise MlflowException(
            "ABFSS URI must be of the form abfss://<filesystem>@<account>.dfs.core.windows.net"
        )
    filesystem = match.group(1)
    account_name = match.group(2)
    path = parsed.path
    if path.startswith("/"):
        path = path[1:]
    return filesystem, account_name, path


def _get_data_lake_client(account_url, credential):
    from azure.storage.filedatalake import DataLakeServiceClient

    return DataLakeServiceClient(account_url, credential)


class AzureDataLakeArtifactRepository(ArtifactRepository):
    """
    Stores artifacts on Azure Data Lake Storage Gen2.

    This repository is used with URIs of the form
    ``abfs[s]://file_system@account_name.dfs.core.windows.net/<path>/<path>``.

    :param credential: Azure credential (see options in https://learn.microsoft.com/en-us/python/api/azure-core/azure.core.credentials?view=azure-python)
                       to use to authenticate to storage
    """

    def __init__(self, artifact_uri, credential):
        super().__init__(artifact_uri)
        _DEFAULT_TIMEOUT = 600  # 10 minutes
        self.write_timeout = MLFLOW_ARTIFACT_UPLOAD_DOWNLOAD_TIMEOUT.get() or _DEFAULT_TIMEOUT

        (filesystem, account_name, path) = _parse_abfss_uri(artifact_uri)

        account_url = f"https://{account_name}.dfs.core.windows.net"
        data_lake_client = _get_data_lake_client(account_url=account_url, credential=credential)
        self.fs_client = data_lake_client.get_file_system_client(filesystem)
        self.base_data_lake_directory = path

    def log_artifact(self, local_file, artifact_path=None):
        raise NotImplementedError(
            "This artifact repository does not support logging single artifacts"
        )

    def log_artifacts(self, local_dir, artifact_path=None):
        dest_path = self.base_data_lake_directory
        if artifact_path:
            dest_path = posixpath.join(dest_path, artifact_path)
        dir_client = self.fs_client.get_directory_client(dest_path)
        local_dir = os.path.abspath(local_dir)
        for root, _, filenames in os.walk(local_dir):
            rel_path = os.path.relpath(root, local_dir)
            for f in filenames:
                # TODO: can base directory client get file at path/to/directory? or do we need
                # a new directory client per local `root` directory that we walk in os.walk?
                file_client = dir_client.get_file_client(posixpath.join(rel_path, f))
                local_file_path = os.path.join(root, f)
                if os.path.getsize(local_file_path) == 0:
                    file_client.create_file()
                else:
                    with open(local_file_path, "rb") as file:
                        file_client.upload_data(data=file, overwrite=True)

    def list_artifacts(self, path=None):
        directory_to_list = self.base_data_lake_directory
        if path:
            directory_to_list = posixpath.join(directory_to_list, path)
        infos = []
        for result in self.fs_client.get_paths(path=directory_to_list, recursive=False):
            if (
                directory_to_list == result.name
            ):  # result isn't actually a child of the path we're interested in, so skip it
                continue
            if result.is_directory:
                subdir = posixpath.relpath(path=result.name, start=self.base_data_lake_directory)
                if subdir.endswith("/"):
                    subdir = subdir[:-1]
                infos.append(FileInfo(subdir, is_dir=True, file_size=None))
            else:
                file_name = posixpath.relpath(path=result.name, start=self.base_data_lake_directory)
                infos.append(FileInfo(file_name, is_dir=False, file_size=result.content_length))

        # The list_artifacts API expects us to return an empty list if the
        # the path references a single file.
        rel_path = directory_to_list[len(self.base_data_lake_directory) + 1 :]
        if (len(infos) == 1) and not infos[0].is_dir and (infos[0].path == rel_path):
            return []
        return sorted(infos, key=lambda f: f.path)

    def _download_file(self, remote_file_path, local_path):
        remote_full_path = posixpath.join(self.base_data_lake_directory, remote_file_path)
        base_dir = posixpath.dirname(remote_full_path)
        dir_client = self.fs_client.get_directory_client(base_dir)
        filename = posixpath.basename(remote_full_path)
        file_client = dir_client.get_file_client(filename)
        with open(local_path, "wb") as file:
            file_client.download_file().readinto(file)

    def delete_artifacts(self, artifact_path=None):
        raise NotImplementedError("This artifact repository does not support deleting artifacts")
