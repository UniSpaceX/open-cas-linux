#
# Copyright(c) 2020-2022 Intel Corporation
# Copyright(c) 2024-2025 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: BSD-3-Clause
#

from datetime import timedelta

from test_tools.common.wait import wait
from core.test_run import TestRun
from test_tools.dd import Dd
from test_tools.fs_tools import create_random_test_file
from test_utils.filesystem.file import File
from type_def.size import Size, Unit


def create_test_files(test_file_size):
    source_file = create_random_test_file("/tmp/source_test_file", test_file_size)
    target_file = File.create_file("/tmp/target_test_file")
    return source_file, target_file


def copy_file(source, target, size, direct=None):
    dd = Dd() \
        .input(source) \
        .output(target) \
        .block_size(Size(1, Unit.Blocks4096)) \
        .count(int(size.get_value(Unit.Blocks4096)))

    if direct == "oflag":
        dd.oflag("direct")
    elif direct == "iflag":
        dd.iflag("direct")
    dd.run()


def compare_files(file1_md5, file2_md5, should_differ=False):
    if should_differ ^ (file1_md5 != file2_md5):
        if should_differ:
            TestRun.fail("Source and target file checksums are identical.")
        else:
            TestRun.fail("Source and target file checksums are different.")


def power_cycle_dut(wait_for_flush_begin=False, core_device=None):
    if wait_for_flush_begin:
        if not core_device:
            raise Exception("Core device is None.")
        TestRun.LOGGER.info("Waiting for flushing to begin...")
        core_writes_before = core_device.get_io_stats().sectors_written
        wait(
            lambda: core_writes_before < core_device.get_io_stats().sectors_written,
            timedelta(minutes=3),
            timedelta(milliseconds=100)
        )
    power_control = TestRun.plugin_manager.get_plugin('power_control')
    power_control.power_cycle(wait_for_connection=True)
