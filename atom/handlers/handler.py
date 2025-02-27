import os
import shutil
import subprocess
import boto3

import bittensor as bt
import mimetypes
from typing import Callable, Optional, Union

from atom.utils import run_command
from atom.chain.chain_utils import json_reader
from abc import ABC, abstractmethod

S3_CONFIG = {
    "region_name": os.getenv("S3_REGION"),
    "endpoint_url": os.getenv("S3_ENDPOINT"),
    "access_key_id": os.getenv("S3_KEY"),
    "secret_access_key": os.getenv("S3_SECRET"),
}

class BaseHandler(ABC):
    @abstractmethod
    def get(self):
        pass

    @abstractmethod
    def put(self):
        pass


class GithubHandler(BaseHandler):
    def __init__(self, repo_url: str):
        self.REPO_URL = repo_url

        self.original_dir = os.getcwd()
        self.repo_name = self.REPO_URL.split("/")[-1].replace(".git", "")
        self.repo_path = os.path.join(self.original_dir, self.repo_name)

    def clone(self):
        """Clones the self.REPO_URL repository into the current directory."""
        if not os.path.exists(self.repo_path):
            try:
                bt.logging.info(f"Cloning repository: {self.REPO_URL}")
                run_command(command=["git", "clone", self.REPO_URL])
            except subprocess.CalledProcessError as e:
                bt.logging.error(f"An error occurred during Git operations: {e}")

    def fetch_all(self):
        """Fetch all changes from self.REPO_URL repository."""
        try:
            bt.logging.info("Fetching latest changes")
            run_command(["git", "fetch", "--all"], cwd=self.repo_path)

        except subprocess.CalledProcessError as e:
            bt.logging.error(f"An error occurred during Git operations: {e}")
            return None

    def get(self, commit_sha: str, filepath: str, reader: Callable = json_reader):
        """Get content from a specific commit in the repository.

        Args:
            commit_sha (str): The commit hash to checkout.
            filepath (str): The path to the file to read. Usually identified through the hotkey, f"{hotkey}.json"
            reader (Callable, optional): Function that reads the datatype specified. Defaults to json_reader.

        Returns:
            content: The content of the file in the specified commit.
        """

        try:
            # Clone and fetch all changes from the repo.
            self.clone()
            self.fetch_all()

            bt.logging.info(f"Checking out commit: {commit_sha}")
            subprocess.run(
                ["git", "checkout", commit_sha], check=True, capture_output=True
            )

            if os.path.exists(filepath):
                bt.logging.info(f"File '{filepath}' found. Reading contents...")
                content = reader(filepath)
                return content
            else:
                bt.logging.error(f"File '{filepath}' not found in this commit.")
                return None

        except subprocess.CalledProcessError as e:
            bt.logging.error(f"An error occurred during Git operations: {e}")
            return None
        except IOError as e:
            bt.logging.error(f"An error occurred while reading the file: {e}")
            return None
        finally:
            if os.path.exists(self.repo_path):
                bt.logging.info(
                    f"Deleting the cloned repository folder: {self.repo_name}"
                )
                shutil.rmtree(self.repo_path)

    def put(
        self,
        content: str,
        folder_name: str,
        file_ext: str,
        hotkey: str,
        branch_name: str = "main",
    ) -> str:
        """Put content into the repository.

        Args:
            content (str): The content to be written into the file.
            folder_name (str): Relative or absolute name of the folder to write the file into.
            file_ext (str): The datatype of the saved file. E.g. "json"
            hotkey (str): Validator hotkey.
            branch_name (str): The branch to commit the changes to. E.g. "main"

        Returns:
            str: _description_
        """

        self.clone()

        # all the operations will be done in the cloned repository folder.
        os.chdir(self.repo_path)

        bt.logging.info(f"Checking out and updating branch: {branch_name}")
        run_command(["git", "checkout", branch_name])
        run_command(["git", "pull", "origin", branch_name])

        # If for any reason the folder to be written into was deleted, create the folder.
        if not os.path.exists(folder_name):
            bt.logging.info(f"Creating folder: {folder_name}")
            os.mkdir(os.path.join(self.repo_path, folder_name))

        filename = os.path.join(folder_name, f"{hotkey}.{file_ext}")

        bt.logging.info(f"Creating file: {filename}")
        with open(filename, "w") as f:
            f.write(content)

        bt.logging.info("Staging, committing, and pushing changes")

        try:
            run_command(["git", "add", filename])
            run_command(["git", "commit", "-m", f"{hotkey} added file"])
            run_command(["git", "push", "origin", branch_name])
        except subprocess.CalledProcessError:
            bt.logging.warning(
                "What you're currently trying to commit has no differences to your last commit. Proceeding with last commit..."
            )

        bt.logging.info("Retrieving commit hash")
        local_commit_hash = run_command(["git", "rev-parse", "HEAD"])

        run_command(["git", "fetch", "origin", branch_name])
        remote_commit_hash = run_command(["git", "rev-parse", f"origin/{branch_name}"])

        if local_commit_hash == remote_commit_hash:
            bt.logging.info(f"Successfully pushed. Commit hash: {local_commit_hash}")
        else:
            bt.logging.warning("Local and remote commit hashes differ.")
            bt.logging.warning(f"Local commit hash: {local_commit_hash}")
            bt.logging.warning(f"Remote commit hash: {remote_commit_hash}")

        os.chdir("..")
        bt.logging.info(f"Deleting the cloned repository folder: {self.repo_name}")
        shutil.rmtree(self.repo_name)

        return remote_commit_hash

def create_s3_client(region_name: str, endpoint_url: str, access_key_id: str, secret_access_key: str) -> boto3.client:
    """
    Creates and returns an S3 client.

    Args:
        region_name (str): The region name
        endpoint_url (str): The endpoint URL
        access_key_id (str): The access key ID
        secret_access_key (str): The secret access key

    Returns:
        boto3.client: An S3 client instance
    """
    return boto3.session.Session().client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )

class S3Handler(BaseHandler):
    """Handles DigitalOcean Spaces S3 operations for content management.

    Manages file content retrieval and storage operations using DigitalOcean Spaces S3.
    """

    s3_client = create_s3_client(**S3_CONFIG)

    def __init__(
        self,
        bucket_name: str,
        s3_client = None,
        custom_mime_types: Optional[dict] = None,
    ):
        """
        Initializes the handler with a bucket name and an s3 client.

        Args:
        bucket_name (str): The name of the s3 bucket to interact with.
        s3_client: The s3 client to interact with the bucket. Defaults to None.
        custom_mime_types (dict[str, str], optional): A dictionary of custom mime types for specific file extensions. Defaults to None.
        """

        self.bucket_name = bucket_name
        self.s3_client = s3_client or self.default_s3_client
        self.custom_mime_types = custom_mime_types or {}

    def put(
        self,
        local_file_path: str,
        s3_bucket_location: str,
        content_type: Optional[str] = None,
        public: bool = False,
    ) -> Union[str, bool]:
        """
        Upload a file to a specific location in the S3 bucket.

        Args:
            local_file_path (str): The local path to the file to upload.
            s3_bucket_location (str): The destination path within the bucket.
            content_type (str, optional): The MIME type of the file. If not provided, inferred from file extension.
            public (bool): Whether to make the uploaded file publicly accessible. Defaults to False.

        Returns:
            Union[str, bool]: The key of the uploaded file if successful, False otherwise.
        """

        try:
            file_name = local_file_path.split("/")[-1]
            key = os.path.join(s3_bucket_location, file_name)

            with open(local_file_path, "rb") as file:
                data = file.read()

            # Infer MIME type
            if not content_type:
                content_type = (
                    self.custom_mime_types.get(file_name[file_name.rfind(".") :])
                    or mimetypes.guess_type(local_file_path)[0]
                    or "application/octet-stream"
                )

            # Upload
            self.s3_client.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
                ACL="public-read" if public else "private",
            )
            return key
        except FileNotFoundError:
            return False
        except Exception as e:
            return False

    def get(self, s3_key: str, local_file_path: str) -> bool:
        """Retrieves a file from the S3 bucket.

        Args:
            s3_key (str): The key of the object in the S3 bucket.
            local_file_path (str): The path where the file should be saved locally.

        Returns:
            bool: True if the file was successfully retrieved and saved, False otherwise.
        """
        try:
            # Download the object from S3 and save it locally
            with open(local_file_path, "wb") as file:
                self.s3_client.s3_client.download_fileobj(
                    self.bucket_name, s3_key, file
                )
            return True
        except self.s3_client.s3_client.exceptions.NoSuchKey:
            return False
        except Exception as e:
            return False
