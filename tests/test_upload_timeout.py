"""123 分片上传的超时与并发异常测试，不访问真实云盘。"""
import os
import tempfile
import threading
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import requests

from pan123 import process_upload_batch, upload_chunk, upload_large_video


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
            failed, uploaded = process_upload_batch(
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
    assert uploaded == []


def test_process_upload_batch_passes_overall_timeout_to_as_completed():
    client = MagicMock()
    client.upload_prepare.return_value = {
        "data": {"presignedUrls": {"1": "https://upload.invalid/part-1"}}
    }
    future = Future()
    executor = MagicMock()
    executor.submit.return_value = future

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"chunk")
        file_path = file.name

    try:
        with patch("pan123.ThreadPoolExecutor", return_value=executor), patch(
            "pan123.as_completed", side_effect=FuturesTimeoutError()
        ) as as_completed_mock:
            failed, uploaded = process_upload_batch(
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
    assert uploaded == []
    as_completed_mock.assert_called_once()
    timeout = as_completed_mock.call_args.kwargs["timeout"]
    assert 0 < timeout <= 1


def test_process_upload_batch_returns_successful_etags():
    client = MagicMock()
    client.upload_prepare.return_value = {
        "data": {"presignedUrls": {"1": "https://upload.invalid/part-1"}}
    }

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"chunk")
        file_path = file.name

    try:
        with patch("pan123.upload_chunk", return_value=(1, "etag-1")):
            failed, uploaded = process_upload_batch(
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

    assert failed == set()
    assert uploaded == [{"PartNumber": 1, "ETag": "etag-1"}]


def test_batch_timeout_cancels_pending_but_collects_running_once():
    class TrackingFuture:
        def __init__(self, result, running):
            self.result_value = result
            self.is_running = running
            self.cancel_calls = 0
            self.result_calls = 0
            self.cancelled = False

        def running(self):
            return self.is_running

        def cancel(self):
            self.cancel_calls += 1
            self.cancelled = True
            return True

        def result(self):
            self.result_calls += 1
            if self.cancelled:
                raise FuturesCancelledError()
            return self.result_value

    class TrackingExecutor:
        def __init__(self):
            self.futures = []

        def submit(self, _function, args, *_extra_args):
            part_num = args[0]
            future = TrackingFuture((part_num, f"etag-{part_num}"), running=part_num == 1)
            self.futures.append(future)
            return future

        def shutdown(self, **_kwargs):
            return None

    client = MagicMock()

    def prepare_upload(data):
        part_num = data["partNumberStart"]
        return {"data": {"presignedUrls": {str(part_num): f"https://upload.invalid/part-{part_num}"}}}

    client.upload_prepare.side_effect = prepare_upload
    executor = TrackingExecutor()

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"0123456789")
        file_path = file.name

    def timeout(_futures, timeout):
        assert timeout > 0
        yield executor.futures[0]
        raise FuturesTimeoutError()

    try:
        with patch("pan123.ThreadPoolExecutor", return_value=executor), patch(
            "pan123.as_completed", side_effect=timeout
        ):
            failed, uploaded = process_upload_batch(
                client=client,
                file_path=file_path,
                upload_data={
                    "Bucket": "bucket",
                    "Key": "key",
                    "StorageNode": "node",
                    "UploadId": "upload-id",
                },
                parts_to_upload={1, 2},
                max_workers=1,
                slice_size=5,
                batch_timeout=1,
            )
    finally:
        os.unlink(file_path)

    assert failed == {2}
    assert uploaded == [{"PartNumber": 1, "ETag": "etag-1"}]
    assert [future.cancel_calls for future in executor.futures] == [0, 1]
    assert [future.result_calls for future in executor.futures] == [1, 1]
    assert [call.args[0]["partNumberStart"] for call in client.upload_prepare.call_args_list] == [1, 2]


def test_batch_timeout_waits_for_blocking_worker_before_returning():
    client = MagicMock()
    client.upload_prepare.return_value = {
        "data": {"presignedUrls": {"1": "https://upload.invalid/part-1"}}
    }
    started = threading.Event()
    release = threading.Event()
    result = {}

    def blocking_worker(*args):
        started.set()
        release.wait(timeout=2)
        return 1, None

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"chunk")
        file_path = file.name

    def timeout(_futures, timeout):
        assert timeout > 0
        assert started.wait(timeout=1), "上传 worker 应已启动"
        raise FuturesTimeoutError()

    def run_batch():
        with patch("pan123.upload_chunk", side_effect=blocking_worker) as upload_mock, patch(
            "pan123.as_completed", side_effect=timeout
        ):
            result["failed"], result["uploaded"] = process_upload_batch(
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
            result["calls"] = upload_mock.call_count

    worker = threading.Thread(target=run_batch)
    worker.start()
    try:
        assert started.wait(timeout=1), "上传 worker 应已启动"
        time.sleep(0.05)
        assert worker.is_alive(), "批次超时后必须等待原 worker 终止，不能立即返回重投"
    finally:
        release.set()
        worker.join(timeout=2)
        os.unlink(file_path)

    assert not worker.is_alive(), "原上传 worker 应被清理"
    assert result["failed"] == {1}
    assert result["uploaded"] == []
    assert result["calls"] == 1


def test_upload_large_video_merges_new_etags_with_resumed_parts():
    client = MagicMock()
    client.upload_request.return_value = {
        "data": {
            "Bucket": "bucket",
            "Key": "key",
            "StorageNode": "node",
            "UploadId": "upload-id",
            "SliceSize": 5,
        }
    }
    client.upload_list.return_value = {
        "data": {"Parts": [{"PartNumber": 1, "ETag": "old-etag"}]}
    }
    client.upload_complete.return_value = {"status": "complete"}

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"0123456789")
        file_path = file.name

    try:
        with patch(
            "pan123.process_upload_batch",
            return_value=(set(), [{"PartNumber": 2, "ETag": "new-etag"}]),
        ) as process_batch:
            result = upload_large_video(
                client=client,
                file_path=file_path,
                enable_resume=True,
                chunk_retry_rounds=0,
                file_name="file.bin",
                file_size=10,
                file_md5="file-md5",
            )
    finally:
        os.unlink(file_path)

    assert result == {"status": "complete"}
    process_batch.assert_called_once()
    complete_payload = client.upload_complete.call_args.args[0]
    assert complete_payload["parts"] == [
        {"PartNumber": 1, "ETag": "old-etag"},
        {"PartNumber": 2, "ETag": "new-etag"},
    ]


def test_upload_large_video_merges_retry_round_etags_with_all_parts():
    client = MagicMock()
    client.upload_request.return_value = {
        "data": {
            "Bucket": "bucket",
            "Key": "key",
            "StorageNode": "node",
            "UploadId": "upload-id",
            "SliceSize": 5,
        }
    }
    client.upload_list.return_value = {
        "data": {"Parts": [{"PartNumber": 1, "ETag": "resumed-etag"}]}
    }
    client.upload_complete.return_value = {"status": "complete"}

    with tempfile.NamedTemporaryFile(delete=False) as file:
        file.write(b"0123456789abcde")
        file_path = file.name

    try:
        with patch(
            "pan123.process_upload_batch",
            side_effect=[
                (
                    {3},
                    [{"PartNumber": 2, "ETag": "first-round-etag"}],
                ),
                (
                    set(),
                    [{"PartNumber": 3, "ETag": "retry-round-etag"}],
                ),
            ],
        ) as process_batch:
            result = upload_large_video(
                client=client,
                file_path=file_path,
                enable_resume=True,
                chunk_retry_rounds=1,
                file_name="file.bin",
                file_size=15,
                file_md5="file-md5",
            )
    finally:
        os.unlink(file_path)

    assert result == {"status": "complete"}
    assert process_batch.call_count == 2
    assert process_batch.call_args_list[0].kwargs["parts_to_upload"] == {2, 3}
    assert process_batch.call_args_list[1].kwargs["parts_to_upload"] == {3}
    complete_payload = client.upload_complete.call_args.args[0]
    assert complete_payload["parts"] == [
        {"PartNumber": 1, "ETag": "resumed-etag"},
        {"PartNumber": 2, "ETag": "first-round-etag"},
        {"PartNumber": 3, "ETag": "retry-round-etag"},
    ]
