#
# Copyright(c) 2020-2022 Intel Corporation
# Copyright(c) 2023-2025 Huawei Technologies
# SPDX-License-Identifier: BSD-3-Clause
#

import posixpath
import pytest

from datetime import timedelta

from api.cas import casadm, casadm_parser, cli
from api.cas.cache_config import CacheMode, CleaningPolicy, CacheModeTrait
from api.cas.casadm_parser import wait_for_flushing
from api.cas.cli import attach_cache_cmd
from connection.utils.output import CmdException
from core.test_run import TestRun
from storage_devices.disk import DiskType, DiskTypeSet, DiskTypeLowerThan
from storage_devices.nullblk import NullBlk
from test_tools.dd import Dd
from test_tools.fio.fio import Fio
from test_tools.fio.fio_param import IoEngine, ReadWrite
from test_tools.fs_tools import Filesystem, create_random_test_file
from test_tools.os_tools import DropCachesMode, sync, drop_caches
from test_tools.udev import Udev
from type_def.size import Size, Unit
from tests.lazy_writes.recovery.recovery_tests_methods import compare_files
from test_utils.filesystem.file import File

mount_point = "/mnt/cas"
test_file_path = f"{mount_point}/test_file"


@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.parametrizex("filesystem", Filesystem)
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_interrupt_core_flush(cache_mode: CacheMode, filesystem: Filesystem):
    """
    title: Test for core's flush interruption.
    description: |
        Test the ability to handle core flush interruption.
    pass_criteria:
      - No system crash.
      - Flushing would be stopped after interruption.
      - Checksum are correct during all test steps.
      - Dirty blocks quantity after interruption is equal or lower but non-zero.
    """
    cache_size = Size(2, Unit.GibiByte)
    iterations_per_config = 10

    with TestRun.step("Prepare cache and core devices"):
        cache_dev = TestRun.disks["cache"]
        core_dev = TestRun.disks["core"]

        cache_dev.create_partitions([cache_size])
        core_dev.create_partitions([cache_size * 2])

        cache_part = cache_dev.partitions[0]
        core_part = core_dev.partitions[0]

    with TestRun.step("Disable udev"):
        Udev.disable()

    for _ in TestRun.iteration(
        range(iterations_per_config), f"Reload cache configuration {iterations_per_config} times."
    ):

        with TestRun.step("Start cache"):
            cache = casadm.start_cache(cache_dev=cache_part, cache_mode=cache_mode, force=True)

        with TestRun.step("Disable cleaning"):
            cache.set_cleaning_policy(CleaningPolicy.nop)

        with TestRun.step(f"Create {filesystem} filesystem on core device"):
            core_part.create_filesystem(fs_type=filesystem)

        with TestRun.step("Add core to the cache"):
            core = cache.add_core(core_part)

        with TestRun.step("Mount core"):
            core.mount(mount_point)

        with TestRun.step("Create test file in mount point of exported object"):
            bs = Size(512, Unit.KibiByte)
            count = int(cache_size.value / bs.value)
            test_file = File.create_file(test_file_path)

            dd = (
                Dd()
                .input("/dev/zero")
                .output(test_file_path)
                .block_size(bs)
                .count(count)
                .oflag("direct")
            )
            dd.run()

            test_file.refresh_item()

        with TestRun.step("Calculate checksum of test file"):
            test_file_checksum_before = test_file.crc32sum()

        with TestRun.step("Get number of dirty data on exported object before interruption"):
            sync()
            drop_caches(DropCachesMode.ALL)
            core_dirty_blocks_before = core.get_dirty_blocks()

        with TestRun.step("Start flushing core device"):
            flush_pid = TestRun.executor.run_in_background(
                cli.flush_core_cmd(str(cache.cache_id), str(core.core_id))
            )

        with TestRun.step("Interrupt core flushing"):
            wait_for_flushing(cache=cache, core=core)
            percentage = casadm_parser.get_flushing_progress(
                cache_id=cache.cache_id, core_id=core.core_id
            )

            while percentage < 50:
                percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)

            TestRun.executor.kill_process(pid=flush_pid)

        with TestRun.step("Check number of dirty data on exported object after interruption"):
            core_dirty_blocks_after = core.get_dirty_blocks()
            if core_dirty_blocks_after >= core_dirty_blocks_before:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after core flush interruption should be lower."
                )
            if core_dirty_blocks_after == Size.zero():
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after core flush interruption should not be zero."
                )

        with TestRun.step("Unmount core and stop cache"):
            core.unmount()
            cache.stop()

        with TestRun.step("Mount core device"):
            core_part.mount(mount_point)

        with TestRun.step("Compare checksum of test file"):
            if test_file_checksum_before != test_file.crc32sum():
                TestRun.LOGGER.error(
                    "Checksum before and after interrupting core flush are different."
                )

        with TestRun.step("Unmount core device"):
            core_part.unmount()


@pytest.mark.parametrizex("filesystem", Filesystem)
@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_interrupt_cache_flush(cache_mode: CacheMode, filesystem: Filesystem):
    """
    title: Test if OpenCAS works correctly after cache's flushing interruption.
    description: |
      Negative test of the ability of OpenCAS to handle cache flushing interruption.
    pass_criteria:
      - No system crash.
      - Flushing would be stopped after interruption.
      - Checksum are correct during all test steps.
      - Dirty blocks quantity after interruption is equal or lower but non-zero.
    """
    cache_size = Size(2, Unit.GibiByte)
    iterations_per_config = 10

    with TestRun.step("Prepare cache and core"):
        cache_dev = TestRun.disks["cache"]
        core_dev = TestRun.disks["core"]

        cache_dev.create_partitions([cache_size])
        core_dev.create_partitions([cache_size * 2])

        cache_part = cache_dev.partitions[0]
        core_part = core_dev.partitions[0]

    with TestRun.step("Disable udev"):
        Udev.disable()

    for _ in TestRun.iteration(
        range(iterations_per_config), f"Reload cache configuration {iterations_per_config} times"
    ):

        with TestRun.step("Start cache"):
            cache = casadm.start_cache(cache_dev=cache_part, cache_mode=cache_mode, force=True)

        with TestRun.step("Set cleaning policy to NOP"):
            cache.set_cleaning_policy(CleaningPolicy.nop)

        with TestRun.step(f"Create {filesystem} filesystem on core device"):
            core_part.create_filesystem(fs_type=filesystem)

        with TestRun.step("Add core to the cache"):
            core = cache.add_core(core_part)

        with TestRun.step("Mount core"):
            core.mount(mount_point)

        with TestRun.step("Create test file in mount point of exported object"):
            bs = Size(512, Unit.KibiByte)
            count = int(cache_size.value / bs.value)
            test_file = File.create_file(test_file_path)

            dd = (
                Dd()
                .input("/dev/zero")
                .output(test_file_path)
                .block_size(bs)
                .count(count)
                .oflag("direct")
            )
            dd.run()

            test_file.refresh_item()

        with TestRun.step("Check checksum of test file"):
            test_file_checksum_before = test_file.crc32sum()

        with TestRun.step("Get number of dirty data on exported object before interruption"):
            sync()
            drop_caches(DropCachesMode.ALL)
            cache_dirty_blocks_before = cache.get_dirty_blocks()

        with TestRun.step("Start flushing cache"):
            flush_pid = TestRun.executor.run_in_background(cli.flush_cache_cmd(str(cache.cache_id)))

        with TestRun.step("Interrupt cache flushing"):
            wait_for_flushing(cache, core)
            percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            while percentage < 20:
                percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            TestRun.executor.kill_process(pid=flush_pid)

        with TestRun.step("Check number of dirty data on exported object after interruption"):
            cache_dirty_blocks_after = cache.get_dirty_blocks()
            if cache_dirty_blocks_after >= cache_dirty_blocks_before:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache flush interruption should be lower."
                )
            if cache_dirty_blocks_after == Size.zero():
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache flush interruption should not be zero."
                )

        with TestRun.step("Unmount core and stop cache"):
            core.unmount()
            cache.stop()

        with TestRun.step("Mount core device"):
            core_part.mount(mount_point)

        with TestRun.step("Check checksum of test file again"):
            if test_file_checksum_before != test_file.crc32sum():
                TestRun.LOGGER.error(
                    "Checksum before and after interrupting cache flush are different."
                )

        with TestRun.step("Unmount core device"):
            core_part.unmount()


@pytest.mark.parametrizex("filesystem", Filesystem)
@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_interrupt_core_remove(cache_mode, filesystem):
    """
    title: Core removal interruption.
    description: |
      Test for proper handling of 'core remove' operation interruption.
    pass_criteria:
      - No system crash.
      - Core would not be removed from cache after interruption.
      - Flushing would be stopped after interruption.
      - Checksums are correct during all test steps.
      - Dirty blocks quantity after interruption is lower but non-zero.
    """
    cache_size = Size(2, Unit.GibiByte)
    iterations_per_config = 10

    with TestRun.step("Prepare cache and core devices"):
        cache_dev = TestRun.disks["cache"]
        cache_dev.create_partitions([cache_size])
        cache_part = cache_dev.partitions[0]
        core_dev = TestRun.disks["core"]
        core_dev.create_partitions([cache_size * 2])
        core_part = core_dev.partitions[0]

    for _ in TestRun.iteration(
        range(iterations_per_config), f"Reload cache configuration {iterations_per_config} times"
    ):
        with TestRun.step("Start cache"):
            cache = casadm.start_cache(cache_part, cache_mode, force=True)

        with TestRun.step("Set cleaning policy to NOP"):
            cache.set_cleaning_policy(CleaningPolicy.nop)

        with TestRun.step(f"Add core device with {filesystem} filesystem and mount it"):
            core_part.create_filesystem(filesystem)
            core = cache.add_core(core_part)
            core.mount(mount_point)

        with TestRun.step("Create test file in mount point of exported object"):
            bs = Size(512, Unit.KibiByte)
            count = int(cache_size.value / bs.value)
            test_file = File.create_file(test_file_path)

            dd = (
                Dd()
                .input("/dev/zero")
                .output(test_file_path)
                .block_size(bs)
                .count(count)
                .oflag("direct")
            )
            dd.run()

            test_file.refresh_item()

        with TestRun.step("Calculate checksum of test file"):
            test_file_checksum_before = test_file.crc32sum()

        with TestRun.step(
            "Get number of dirty data on exported object before core removal interruption"
        ):
            sync()
            drop_caches(DropCachesMode.ALL)
            cache_dirty_blocks_before = cache.get_dirty_blocks()

        with TestRun.step("Unmount core"):
            core.unmount()

        with TestRun.step("Start removing core"):
            flush_pid = TestRun.executor.run_in_background(
                cli.remove_core_cmd(str(cache.cache_id), str(core.core_id))
            )

        with TestRun.step("Interrupt core removing"):
            wait_for_flushing(cache, core)
            percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            while percentage < 50:
                percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            TestRun.executor.run(f"kill -s SIGINT {flush_pid}")

        with TestRun.step(
            "Check number of dirty data on exported object after core removal interruption"
        ):
            cache_dirty_blocks_after = cache.get_dirty_blocks()
            if cache_dirty_blocks_after >= cache_dirty_blocks_before:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after core removal interruption should be lower."
                )
            if int(cache_dirty_blocks_after) == 0:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after core removal interruption should not be zero."
                )

        with TestRun.step("Mount core and verify test file checksum after interruption"):
            core.mount(mount_point)

            if test_file.crc32sum() != test_file_checksum_before:
                TestRun.LOGGER.error("Checksum after interrupting core removal is different.")

        with TestRun.step("Unmount core"):
            core.unmount()

        with TestRun.step("Stop cache"):
            cache.stop()

        with TestRun.step("Mount core device"):
            core_part.mount(mount_point)

        with TestRun.step("Verify checksum of test file again"):
            if test_file.crc32sum() != test_file_checksum_before:
                TestRun.LOGGER.error("Checksum after core removal is different.")

        with TestRun.step("Unmount core device"):
            core_part.unmount()


@pytest.mark.parametrize("stop_percentage", [0, 50])
@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_interrupt_cache_mode_switch_parametrized(cache_mode, stop_percentage):
    """
    title: Test if OpenCAS works correctly after cache mode switching
    immediate or delayed interruption.
    description: |
      Negative test of the ability of OpenCAS to handle cache mode switching
      immediate or delayed interruption.
    pass_criteria:
      - No system crash.
      - Cache mode will not be switched after interruption.
      - Flushing would be stopped after interruption.
      - Checksum are correct during all test steps.
      - Dirty blocks quantity after interruption is lower but non-zero.
    """
    cache_size = Size(2, Unit.GibiByte)

    iterations_per_config = 10
    test_file_size = Size(1, Unit.GibiByte)
    test_file_path = "/mnt/cas/test_file"

    with TestRun.step("Prepare cache and core."):
        cache_dev = TestRun.disks["cache"]
        core_dev = TestRun.disks["core"]

        cache_dev.create_partitions([cache_size])
        core_dev.create_partitions([cache_size * 2])

        cache_part = cache_dev.partitions[0]
        core_part = core_dev.partitions[0]

    with TestRun.step("Disable udev"):
        Udev.disable()

    for _ in TestRun.iteration(
        range(iterations_per_config), f"Reload cache configuration {iterations_per_config} times."
    ):

        with TestRun.step("Start cache"):
            cache = casadm.start_cache(cache_part, cache_mode, force=True)

        with TestRun.step("Set cleaning policy to NOP"):
            cache.set_cleaning_policy(CleaningPolicy.nop)

        with TestRun.step("Add core device"):
            core = cache.add_core(core_part)

        with TestRun.step("Create test file in mount point of exported object"):
            test_file = create_random_test_file(test_file_path, test_file_size)

        with TestRun.step("Calculate checksum of test file"):
            test_file_checksum_before = test_file.crc32sum()

        with TestRun.step("Copy test data to core"):
            dd = (
                Dd()
                .block_size(test_file_size)
                .input(test_file.full_path)
                .output(core.path)
                .oflag("direct")
            )
            dd.run()

        with TestRun.step("Get number of dirty data on exported object before interruption"):
            sync()
            drop_caches(DropCachesMode.ALL)
            cache_dirty_blocks_before = cache.get_dirty_blocks()

        with TestRun.step("Start switching cache mode"):
            flush_pid = TestRun.executor.run_in_background(
                cli.set_cache_mode_cmd(
                    cache_mode=str(CacheMode.DEFAULT.name.lower()),
                    cache_id=str(cache.cache_id),
                    flush_cache="yes",
                )
            )

        with TestRun.step("Kill flush process during cache flush operation"):
            wait_for_flushing(cache, core)
            percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            while percentage < stop_percentage:
                percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            TestRun.executor.kill_process(flush_pid)

        with TestRun.step("Check number of dirty data on exported object after interruption"):
            cache_dirty_blocks_after = cache.get_dirty_blocks()
            if cache_dirty_blocks_after >= cache_dirty_blocks_before:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache mode switching "
                    "interruption should be lower."
                )
            if cache_dirty_blocks_after == Size.zero():
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache mode switching "
                    "interruption should not be zero."
                )

        with TestRun.step("Check cache mode"):
            if cache.get_cache_mode() != cache_mode:
                TestRun.LOGGER.error("Cache mode should remain the same.")

        with TestRun.step("Stop cache"):
            cache.stop()

        with TestRun.step("Copy test data from the exported object to a file"):
            dd = (
                Dd()
                .block_size(test_file_size)
                .input(core.path)
                .output(test_file.full_path)
                .oflag("direct")
            )
            dd.run()

        with TestRun.step("Compare checksum of test files"):
            test_file_checksum_after = test_file.crc32sum()
            compare_files(test_file_checksum_before, test_file_checksum_after)


@pytest.mark.parametrizex("filesystem", Filesystem)
@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_interrupt_cache_stop(cache_mode, filesystem):
    """
    title: Test if OpenCAS works correctly after cache stopping interruption.
    description: |
        Test the ability to handle cache's stop interruption.
    pass_criteria:
      - No system crash.
      - Flushing would be stopped after interruption.
      - Checksum are correct during all test steps.
      - Dirty blocks quantity after interruption is lower but non-zero.
    """
    cache_size = Size(2, Unit.GibiByte)
    iterations_per_config = 10

    with TestRun.step("Prepare cache and core devices"):
        cache_dev = TestRun.disks["cache"]
        core_dev = TestRun.disks["core"]

        cache_dev.create_partitions([cache_size])
        core_dev.create_partitions([cache_size * 2])

        cache_part = cache_dev.partitions[0]
        core_part = core_dev.partitions[0]

    with TestRun.step("Disable udev"):
        Udev.disable()

    for _ in TestRun.iteration(
        range(iterations_per_config), f"Reload cache configuration {iterations_per_config} times."
    ):

        with TestRun.step("Start cache"):
            cache = casadm.start_cache(cache_dev=cache_part, cache_mode=cache_mode, force=True)

        with TestRun.step("Set cleaning policy to NOP"):
            cache.set_cleaning_policy(cleaning_policy=CleaningPolicy.nop)

        with TestRun.step(f"Add core device with {filesystem} filesystem and mount it"):
            core_part.create_filesystem(fs_type=filesystem)
            core = cache.add_core(core_dev=core_part)
            core.mount(mount_point=mount_point)

        with TestRun.step("Create test file in mount point of exported object"):
            bs = Size(512, Unit.KibiByte)
            count = int(cache_size.value / bs.value)
            test_file = File.create_file(test_file_path)

            dd = (
                Dd()
                .input("/dev/zero")
                .output(test_file_path)
                .block_size(bs)
                .count(count)
                .oflag("direct")
            )
            dd.run()

            test_file.refresh_item()

        with TestRun.step("Calculate checksum of test file"):
            test_file_checksum_before = test_file.crc32sum()

        with TestRun.step("Get number of dirty data on exported object before interruption"):
            sync()
            drop_caches(DropCachesMode.ALL)
            cache_dirty_blocks_before = cache.get_dirty_blocks()

        with TestRun.step("Unmount core"):
            core.unmount()

        with TestRun.step("Start stopping cache"):
            flush_pid = TestRun.executor.run_in_background(cli.stop_cmd(str(cache.cache_id)))

        with TestRun.step("Interrupt cache stopping"):
            wait_for_flushing(cache, core)
            percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
            while percentage < 50:
                percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)

            TestRun.executor.kill_process(pid=flush_pid)

        with TestRun.step("Check number of dirty data on exported object after interruption"):
            cache_dirty_blocks_after = cache.get_dirty_blocks()
            if cache_dirty_blocks_after >= cache_dirty_blocks_before:
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache stop interruption should be lower."
                )
            if cache_dirty_blocks_after == Size.zero():
                TestRun.LOGGER.error(
                    "Quantity of dirty lines after cache stop interruption should not be zero."
                )

        with TestRun.step("Stop cache"):
            cache.stop()

        with TestRun.step("Mount core device"):
            core_part.mount(mount_point=mount_point)

        with TestRun.step("Compare checksum of test files"):
            test_file_checksum_after = test_file.crc32sum()
            compare_files(test_file_checksum_before, test_file_checksum_after)

        with TestRun.step("Unmount core device"):
            core_part.unmount()


@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.nand, DiskType.optane]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
@pytest.mark.parametrizex("cache_mode", CacheMode)
def test_interrupt_attach(cache_mode):
    """
    title: Test for attach interruption.
    description: Validate handling interruption of cache attach.
    pass_criteria:
      - No crash during attach interruption.
      - Cache attach completed successfully.
      - No system crash.
    """

    with TestRun.step("Prepare cache and core devices"):
        nullblk = NullBlk.create(size_gb=1500)
        cache_dev = nullblk[0]
        core_dev = TestRun.disks["core"]
        core_dev.create_partitions([Size(2, Unit.GibiByte)])
        core_dev = core_dev.partitions[0]

    with TestRun.step("Start cache and add core"):
        cache = casadm.start_cache(cache_dev, force=True, cache_mode=cache_mode)
        cache.add_core(core_dev)

    with TestRun.step(f"Change cache mode to {cache_mode}"):
        cache.set_cache_mode(cache_mode)

    with TestRun.step("Detach cache"):
        cache.detach()

    with TestRun.step("Start attaching cache in background"):
        cache_attach_pid = TestRun.executor.run_in_background(
            attach_cache_cmd(cache_id=str(cache.cache_id), cache_dev=cache_dev.path)
        )

    with TestRun.step("Try to interrupt cache attaching"):
        TestRun.executor.kill_process(cache_attach_pid)

    with TestRun.step("Wait for cache attach to end"):
        TestRun.executor.wait_cmd_finish(
            cache_attach_pid, timeout=timedelta(minutes=10)
        )

    with TestRun.step("Verify if cache attach ended successfully"):
        caches = casadm_parser.get_caches()
        if len(caches) != 1:
            TestRun.fail(f"Wrong amount of caches: {len(caches)}, expected: 1")
        if caches[0].cache_device.path == cache_dev.path:
            TestRun.LOGGER.info("Operation ended successfully")
        else:
            TestRun.fail(
                "Cache attaching failed"
                "expected behaviour: attach completed successfully"
                "actual behaviour: attach interrupted"
            )


@pytest.mark.parametrizex("filesystem", Filesystem)
@pytest.mark.parametrizex("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
@pytest.mark.require_disk("core", DiskTypeLowerThan("cache"))
def test_detach_interrupt_cache_flush(filesystem, cache_mode):
    """
    title: Test for flush interruption using cache detach operation.
    description: Validate handling detach during cache flush.
    pass_criteria:
      - No system crash.
      - Detach operation doesn't stop cache flush.
    """

    with TestRun.step("Prepare cache and core devices"):
        cache_dev = TestRun.disks["cache"]
        cache_dev.create_partitions([Size(2, Unit.GibiByte)])
        cache_dev = cache_dev.partitions[0]

        core_dev = TestRun.disks["core"]
        core_dev.create_partitions([Size(5, Unit.GibiByte)])
        core_dev = core_dev.partitions[0]

    with TestRun.step("Disable udev"):
        Udev.disable()

    with TestRun.step("Start cache"):
        cache = casadm.start_cache(cache_dev, force=True, cache_mode=cache_mode)

    with TestRun.step(f"Change cache mode to {cache_mode}"):
        cache.set_cache_mode(cache_mode)

    with TestRun.step(f"Add core device with {filesystem} filesystem and mount it"):
        core_dev.create_filesystem(filesystem)
        core = cache.add_core(core_dev)
        core.mount(mount_point)

    with TestRun.step("Populate cache with dirty data"):
        fio = (
            Fio()
            .create_command()
            .size(Size(4, Unit.GibiByte))
            .read_write(ReadWrite.randrw)
            .io_engine(IoEngine.libaio)
            .block_size(Size(1, Unit.Blocks4096))
            .target(posixpath.join(mount_point, "test_file"))
        )
        fio.run()

        if cache.get_dirty_blocks() <= Size.zero():
            TestRun.fail("Failed to populate cache with dirty data")
        if core.get_dirty_blocks() <= Size.zero():
            TestRun.fail("There is no dirty data on core")

    with TestRun.step("Start flushing cache"):
        flush_pid = TestRun.executor.run_in_background(
            cli.flush_cache_cmd(str(cache.cache_id))
        )

    with TestRun.step("Interrupt cache flushing by cache detach"):
        wait_for_flushing(cache, core)
        percentage = casadm_parser.get_flushing_progress(cache.cache_id, core.core_id)
        while percentage < 50:
            percentage = casadm_parser.get_flushing_progress(
                cache.cache_id, core.core_id
            )

    with TestRun.step("Detach cache"):
        try:
            cache.detach()
            TestRun.fail("Cache detach during flush succeed, expected: fail")
        except CmdException:
            TestRun.LOGGER.info(
                "Cache detach during flush failed, as expected"
            )
        TestRun.executor.wait_cmd_finish(flush_pid)
        cache.detach()
