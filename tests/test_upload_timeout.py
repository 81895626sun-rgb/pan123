"""123 分片上传的超时与并发异常测试，不访问真实云盘。"""
import os
import tempfile
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import requests

from pan123 import process_upload_batch, upload_chunk


def test_upload_chunk_retries_timeout_with_connect_and_read_timeout():
    response = MagicMock(status_code=200, headers={"ETag": '"etag-1"'})

    with patch("pan123.requests.put", side_effect=[requests.Timeout("read timeout"), response]) as put:
        result = upload_chunk(
            (1, b"chunk", "https://upload.invalid/part-1", 2),
            connect_timeout=2,
            read_timeout=7,
        )

    assert result == (1, "etag-1")
    assert put.call_count == 2
    assert put.call_args.kwargs["timeout"] == (2, 7)


def test_process_upload_batch_marks_future_exception_as_failed():
    client = MagicMock()
    client.upload_prepare.return_value = {
        "data": {"presignedUrls": {"1": "https://upload.invalid/part-1"}}
    }

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"chunk")
        file_path = file.name

    try:
        with patch("pan123.upload_chunk", side_effect=RuntimeError("worker failed")):
            failed = process_upload_batch(
                client=client,
                file_path=file_path,
                upload_data={
                    "Bucket": "bucket",
                    "Key": "key",
                    "StorageNode": "node",
                    "UploadId": "upload-id",
                },
                parts_to_upload={1},
                max_workers=1,
                slice_size=5,
                batch_timeout=1,
            )
    finally:
        os.unlink(file_path)

    assert failed == {1}


def test_process_upload_batch_passes_overall_timeout_to_as_completed():
    client = MagicMock()
    client.upload_prepare.return_value = {
        "data": {"presignedUrls": {"1": "https://upload.invalid/part-1"}}
    }

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"chunk")
        file_path = file.name

    try:
        with patch("pan123.upload_chunk", return_value=(1, "etag-1")), patch(
            "pan123.as_completed", side_effect=FuturesTimeoutError()
        ) as as_completed_mock:
            failed = process_upload_batch(
                client=client,
                file_path=file_path,
                upload_data={
                    "Bucket": "bucket",
                    "Key": "key",
                    "StorageNode": "node",
                    "UploadId": "upload-id",
                },
                parts_to_upload={1},
                max_workers=1,
                slice_size=5,
                batch_timeout=0.01,
            )
    finally:
        os.unlink(file_path)

    assert failed == set()
    as_completed_mock.assert_called_once()
    assert as_completed_mock.call_args.kwargs["timeout"] == 0.01
