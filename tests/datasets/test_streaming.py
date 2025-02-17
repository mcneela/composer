import math
import os
import pathlib
import shutil
import time
from typing import Any, Callable, Dict, List, Tuple

import numpy as np
import pytest
from torch.utils.data import DataLoader

from composer.datasets.streaming import StreamingDataset, StreamingDatasetWriter
from composer.utils import dist


@pytest.fixture
def remote_local(tmp_path: pathlib.Path) -> Tuple[str, str]:
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    return str(remote), str(local)


def get_fake_samples_decoders(num_samples: int) -> Tuple[List[Dict[str, bytes]], Dict[str, Callable[[bytes], Any]]]:
    samples = [{"uid": f"{ix:06}".encode("utf-8"), "data": (3 * ix).to_bytes(4, "big")} for ix in range(num_samples)]
    decoders = {
        "uid": lambda uid_bytes: uid_bytes.decode("utf-8"),
        "data": lambda data_bytes: int.from_bytes(data_bytes, "big")
    }
    return samples, decoders


def write_synthetic_streaming_dataset(dirname: str, samples: List[Dict[str, bytes]], shard_size_limit: int) -> None:
    first_sample_fields = list(samples[0].keys())
    with StreamingDatasetWriter(dirname=dirname, fields=first_sample_fields,
                                shard_size_limit=shard_size_limit) as writer:
        writer.write_samples(samples=samples)


@pytest.mark.parametrize("num_samples", [100, 10000])
@pytest.mark.parametrize("shard_size_limit", [1 << 8, 1 << 16, 1 << 24])
def test_writer(remote_local: Tuple[str, str], num_samples: int, shard_size_limit: int) -> None:
    dirname, _ = remote_local
    samples, _ = get_fake_samples_decoders(num_samples)

    first_sample_values = samples[0].values()
    first_sample_byte_sizes = np.array([len(v) for v in first_sample_values], dtype=np.int64)
    first_sample_bytes = len(first_sample_byte_sizes.tobytes() + b''.join(first_sample_values))

    expected_samples_per_shard = shard_size_limit // first_sample_bytes
    expected_num_shards = math.ceil(num_samples / expected_samples_per_shard)
    expected_num_files = expected_num_shards + 1  # the index file

    write_synthetic_streaming_dataset(dirname=dirname, samples=samples, shard_size_limit=shard_size_limit)
    files = os.listdir(dirname)

    assert len(files) == expected_num_files, f"Files written ({len(files)}) != expected ({expected_num_files})."


@pytest.mark.parametrize("batch_size", [None, 1, 2])
@pytest.mark.parametrize("share_remote_local", [False, True])
@pytest.mark.parametrize("shuffle", [False, True])
def test_reader(remote_local: Tuple[str, str], batch_size: int, share_remote_local: bool, shuffle: bool):
    num_samples = 117
    shard_size_limit = 1 << 8
    samples, decoders = get_fake_samples_decoders(num_samples)
    remote, local = remote_local
    if share_remote_local:
        local = remote
    write_synthetic_streaming_dataset(dirname=remote, samples=samples, shard_size_limit=shard_size_limit)

    # Build StreamingDataset
    dataset = StreamingDataset(remote=remote, local=local, shuffle=shuffle, decoders=decoders, batch_size=batch_size)

    # Test basic sample order
    rcvd_samples = 0
    shuffle_matches = 0
    for ix, sample in enumerate(dataset):
        rcvd_samples += 1
        uid = sample["uid"]
        data = sample["data"]
        expected_uid = f"{ix:06}"
        expected_data = 3 * ix
        if shuffle:
            shuffle_matches += (expected_uid == uid)
        else:
            assert uid == expected_uid == uid, f"sample ix={ix} has uid={uid}, expected {expected_uid}"
            assert data == expected_data, f"sample ix={ix} has data={data}, expected {expected_data}"

    # If shuffling, there should be few matches
    # The probability of k matches in a random permutation is ~1/(e*(k!))
    if shuffle:
        assert shuffle_matches < 10

    # Test length
    assert rcvd_samples == num_samples, f"Only received {rcvd_samples} samples, expected {num_samples}"
    assert len(dataset) == num_samples, f"Got dataset length={len(dataset)} samples, expected {num_samples}"


@pytest.mark.timeout(10)
@pytest.mark.parametrize("created_ago", [0.5, 3])
@pytest.mark.parametrize("timeout", [1])
def test_reader_after_crash(remote_local: Tuple[str, str], created_ago: float, timeout: float):
    num_samples = 117
    shard_size_limit = 1 << 8
    samples, decoders = get_fake_samples_decoders(num_samples)
    remote, local = remote_local
    write_synthetic_streaming_dataset(dirname=remote, samples=samples, shard_size_limit=shard_size_limit)

    shutil.copy(os.path.join(remote, "index.mds"), os.path.join(local, "index.mds.tmp"))
    shutil.copy(os.path.join(remote, "000003.mds"), os.path.join(local, "000003.mds.tmp"))
    time.sleep(created_ago)
    dataset = StreamingDataset(remote=remote, local=local, shuffle=False, decoders=decoders, timeout=timeout)

    # Iterate over dataset and make sure there are no TimeoutErrors
    for _ in dataset:
        pass


@pytest.mark.parametrize(
    "share_remote_local",
    [
        True,
        pytest.param(False, marks=pytest.mark.xfail(reason="__getitem__ currently expects shards to exist")),
    ],
)
def test_reader_getitem(remote_local: Tuple[str, str], share_remote_local: bool):
    num_samples = 117
    shard_size_limit = 1 << 8
    samples, decoders = get_fake_samples_decoders(num_samples)
    remote, local = remote_local
    if share_remote_local:
        local = remote
    write_synthetic_streaming_dataset(dirname=remote, samples=samples, shard_size_limit=shard_size_limit)

    # Build StreamingDataset
    dataset = StreamingDataset(remote=remote, local=local, shuffle=False, decoders=decoders)

    # Test retrieving random sample
    _ = dataset[17]


@pytest.mark.daily()
@pytest.mark.timeout(10)
@pytest.mark.parametrize("batch_size", [1, 2, 5])
@pytest.mark.parametrize("drop_last", [False, True])
@pytest.mark.parametrize("num_workers", [1, 2, 3])
@pytest.mark.parametrize("persistent_workers", [
    False,
    pytest.param(
        True,
        marks=pytest.mark.xfail(
            reason=
            "PyTorch DataLoader has non-deterministic worker cycle iterator when `persistent_workers=True`. Fixed in Mar 2022, likely landing PyTorch 1.12: https://github.com/pytorch/pytorch/pull/73675"
        )),
])
@pytest.mark.parametrize("shuffle", [False, True])
def test_dataloader_single_device(remote_local: Tuple[str, str], batch_size: int, drop_last: bool, num_workers: int,
                                  persistent_workers: bool, shuffle: bool):
    num_samples = 31
    shard_size_limit = 1 << 6
    samples, decoders = get_fake_samples_decoders(num_samples)
    remote, local = remote_local
    write_synthetic_streaming_dataset(dirname=remote, samples=samples, shard_size_limit=shard_size_limit)

    # Build StreamingDataset
    dataset = StreamingDataset(remote=remote, local=local, shuffle=shuffle, decoders=decoders, batch_size=batch_size)

    # Build DataLoader
    dataloader = DataLoader(dataset=dataset,
                            batch_size=batch_size,
                            num_workers=num_workers,
                            drop_last=drop_last,
                            persistent_workers=persistent_workers)

    # Expected number of batches based on batch_size and drop_last
    expected_num_batches = (num_samples // batch_size) if drop_last else math.ceil(num_samples / batch_size)
    expected_num_samples = expected_num_batches * batch_size if drop_last else num_samples

    # Iterate over DataLoader
    rcvd_batches = 0
    sample_order = []

    for batch_ix, batch in enumerate(dataloader):
        rcvd_batches += 1

        # Every batch should be complete except (maybe) final one
        if batch_ix + 1 < expected_num_batches:
            assert len(batch["uid"]) == batch_size
        else:
            if drop_last:
                assert len(batch["uid"]) == batch_size
            else:
                assert len(batch["uid"]) <= batch_size

        for uid in batch["uid"]:
            sample_order.append(int(uid))

    # Test dataloader length
    assert len(dataloader) == expected_num_batches
    assert rcvd_batches == expected_num_batches

    # Test that all samples arrived
    assert len(sample_order) == expected_num_samples
    if not drop_last:
        assert len(set(sample_order)) == num_samples

    # Iterate over the dataloader again to check shuffle behavior
    second_sample_order = []
    for batch_ix, batch in enumerate(dataloader):
        for uid in batch["uid"]:
            second_sample_order.append(int(uid))

    assert len(sample_order) == len(second_sample_order)
    if shuffle:
        assert sample_order != second_sample_order
    else:
        assert sample_order == second_sample_order


@pytest.mark.daily()
@pytest.mark.timeout(10)
@pytest.mark.world_size(2)
@pytest.mark.parametrize("batch_size", [4])
@pytest.mark.parametrize("drop_last", [False, True])
@pytest.mark.parametrize("num_samples", [30, 31])
@pytest.mark.parametrize("num_workers", [1, 3])
@pytest.mark.parametrize("shuffle", [False, True])
def test_dataloader_multi_device(remote_local: Tuple[str, str], batch_size: int, drop_last: bool, num_samples: int,
                                 num_workers: int, shuffle: bool):

    global_device = dist.get_global_rank()
    global_num_devices = dist.get_world_size()
    assert batch_size % global_num_devices == 0
    device_batch_size = batch_size // global_num_devices

    shard_size_limit = 1 << 6
    samples, decoders = get_fake_samples_decoders(num_samples)

    # Broadcast remote, local
    remote_local_list = list(remote_local)
    dist.broadcast_object_list(remote_local_list)
    remote, local = remote_local_list

    # Create dataset on device 0
    if global_device == 0:
        write_synthetic_streaming_dataset(dirname=remote, samples=samples, shard_size_limit=shard_size_limit)
    dist.barrier()

    # Build StreamingDataset
    dataset = StreamingDataset(remote=remote,
                               local=local,
                               shuffle=shuffle,
                               decoders=decoders,
                               batch_size=device_batch_size)

    # Build DataLoader
    dataloader = DataLoader(dataset=dataset,
                            batch_size=device_batch_size,
                            num_workers=num_workers,
                            drop_last=drop_last,
                            persistent_workers=False)

    # Expected number of samples and batches based on global_num_devices, batch_size and drop_last
    device_compatible_num_samples = global_num_devices * math.ceil(num_samples / global_num_devices)
    expected_num_batches = (device_compatible_num_samples //
                            batch_size) if drop_last else math.ceil(device_compatible_num_samples / batch_size)
    expected_num_samples = expected_num_batches * batch_size if drop_last else device_compatible_num_samples

    # Iterate over DataLoader
    rcvd_batches = 0
    sample_order = []

    for batch_ix, batch in enumerate(dataloader):
        rcvd_batches += 1

        # Every batch should be complete except (maybe) final one
        if batch_ix + 1 < expected_num_batches:
            assert len(batch["uid"]) == device_batch_size
        else:
            if drop_last:
                assert len(batch["uid"]) == device_batch_size
            else:
                assert len(batch["uid"]) <= device_batch_size

        device_batch_uids = [int(uid) for uid in batch["uid"]]
        all_device_batch_uids = dist.all_gather_object(device_batch_uids)

        for uids in all_device_batch_uids:
            sample_order += uids

    # Test dataloader length
    assert len(dataloader) == expected_num_batches
    assert rcvd_batches == expected_num_batches

    # Test that all samples arrived
    assert len(sample_order) == expected_num_samples
    if not drop_last:
        assert len(set(sample_order)) == num_samples

    # Iterate over the dataloader again to check shuffle behavior
    second_sample_order = []
    for batch_ix, batch in enumerate(dataloader):
        device_batch_uids = [int(uid) for uid in batch["uid"]]
        all_device_batch_uids = dist.all_gather_object(device_batch_uids)
        for uids in all_device_batch_uids:
            second_sample_order += uids

    assert len(sample_order) == len(second_sample_order)
    if shuffle:
        assert sample_order != second_sample_order
    else:
        assert sample_order == second_sample_order
