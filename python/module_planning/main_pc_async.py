import asyncio
import os
import sys
import time

if os.name == "nt":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)

from pc_transport_node_async import (
    AsyncPCTransportNode,
    TransportProxyRemoteError,
    TransportProxyTimeout,
)


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
TEST_BYTES = int(sys.argv[2]) if len(sys.argv) > 2 else 256 * 1024
STREAM_COMMAND = "stream_test"
STREAM_BYTE = 0xA5
DIAG_RESET_COMMAND = "diag_reset"
DIAG_INFO_COMMAND = "diag_info"
DIAG_READ_COMMAND = "diag_read"
DIAG_CAPTURE_COMMAND = "diag_capture"
DIAG_PAGE_VALUES = 7

CORE_STAT_NAMES = (
    "radio_irqs", "events_emitted", "events_polled", "rx_packets",
    "tx_packets", "event_queue_overflows", "rx_overruns",
    "tx_underruns", "max_rt_events", "max_rt_restarts",
    "protocol_errors", "pipe_rx_bytes", "pipe_tx_bytes",
    "control_queued", "control_acked", "commands_sent",
    "commands_failed", "pipes_opened", "pipes_closed", "pipes_failed",
    "registration_rx", "registration_sent", "registration_failed",
)
DIAG_META_NAMES = (
    "generation", "tag", "ticks_ms", "pipe_id", "submitted",
    "sticky_errors", "schedule_tx_ms", "schedule_rx_ms", "open_wait_ms",
    "max_send_wait_ms", "max_send_wait_at", "wait_ge_20ms",
    "wait_ge_100ms", "wait_ge_500ms", "wait_ge_1000ms",
)
DIAG_TAG_NAMES = {
    1: "baseline",
    2: "pipe_opened",
    3: "data_submitted",
    4: "pipe_closed",
    5: "stream_failed",
    6: "remote_capture",
}


class StreamFailure(RuntimeError):
    pass


class AsyncPCNode(AsyncPCTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_source = None
        self.expected_bytes = 0
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = None
        self.stream_finished_at = None
        self.active_pipe_id = None
        self.failure = None
        self.stream_opened = asyncio.Event()
        self.stream_finished = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.stream_failed = asyncio.Event()
        self.data_gaps = []
        self.last_data_at = None

    async def on_command(self, src_id, command):
        return {"ok": True}

    async def on_pipe_opened(self, pipe_id, src_id):
        if src_id != self.test_source or self.active_pipe_id is not None:
            return
        self.active_pipe_id = pipe_id
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = time.perf_counter()
        self.last_data_at = self.stream_started_at
        self.stream_opened.set()
        print("pipe", pipe_id, "opened from", src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        if src_id != self.test_source or pipe_id != self.active_pipe_id or \
                self.stream_started_at is None:
            return
        remaining = self.expected_bytes - self.received_bytes
        if remaining <= 0:
            return
        data = data_chunk[:remaining]
        now = time.perf_counter()
        if data and self.last_data_at is not None:
            self.data_gaps.append((
                (now - self.last_data_at) * 1000.0,
                self.received_bytes,
                len(data),
            ))
            self.last_data_at = now
        self.received_bytes += len(data)
        self.invalid_bytes += len(data) - data.count(STREAM_BYTE)
        if self.received_bytes >= self.expected_bytes and \
                self.stream_finished_at is None:
            self.stream_finished_at = time.perf_counter()
            print("data complete after {:.3} s".format(
                self.stream_finished_at - self.stream_started_at
            ))
            self.stream_finished.set()

    async def on_pipe_closed(self, pipe_id, src_id):
        if src_id == self.test_source and pipe_id == self.active_pipe_id:
            now = time.perf_counter()
            print("pipe", pipe_id, "closed from", src_id)
            if self.stream_finished_at is not None:
                print("close delay: {:.3f} s".format(
                    now - self.stream_finished_at
                ))
            else:
                self.failure = (
                    "pipe closed before all data", self.received_bytes
                )
                self.stream_failed.set()
            self.stream_closed.set()

    async def on_pipe_failed(self, pipe_id, src_id, reason,
                             transferred_bytes):
        if src_id != self.test_source:
            return
        if self.active_pipe_id is not None and \
                pipe_id != self.active_pipe_id:
            return
        self.failure = ("pipe failed", reason, transferred_bytes)
        print("pipe", pipe_id, "failed from", src_id, "reason", reason,
              "bytes", transferred_bytes)
        self.stream_failed.set()

    def on_callback_error(self, error):
        print("callback error:", error)


def counter_delta(after, before):
    return (after - before) & 0xffffffff


def print_gap_summary(node):
    if not node.data_gaps:
        print("PC gaps: no data callbacks")
        return
    gaps = node.data_gaps
    counts = []
    for threshold in (20, 100, 500, 1000):
        counts.append(sum(1 for gap, unused_offset, unused_size in gaps
                          if gap >= threshold))
    largest = sorted(gaps, reverse=True)[:8]
    print("PC gaps: callbacks", len(gaps), "max_ms",
          "{:.3f}".format(largest[0][0]), "ge20/100/500/1000", counts)
    print("PC largest gaps (ms, byte_offset, callback_bytes):")
    for gap, offset, size in largest:
        print(" ", "{:.3f}".format(gap), offset, size)
    print("PC end offset mod 512/2048:", node.received_bytes % 512,
          node.received_bytes % 2048)


def print_core_diagnostics(label, before, after):
    if before is None or after is None:
        print(label, "core diagnostics unavailable")
        return
    before_stats = before.get("stats", ())
    after_stats = after.get("stats", ())
    print(label, "core before:", before)
    print(label, "core after:", after)
    if len(before_stats) != len(CORE_STAT_NAMES) or \
            len(after_stats) != len(CORE_STAT_NAMES):
        print(label, "unexpected stats length")
        return
    delta = {}
    for index, name in enumerate(CORE_STAT_NAMES):
        delta[name] = counter_delta(after_stats[index], before_stats[index])
    print(label, "core delta:", delta)


async def remote_diag_call(node, target, command):
    try:
        return await node.send_command_and_wait_reply(
            target, command, timeout_ms=4000
        )
    except Exception as error:
        print("slave diag request failed:", command.get("cmd"), error)
        return None


async def collect_slave_diagnostics(node, target):
    info = await remote_diag_call(node, target, {"cmd": DIAG_INFO_COMMAND})
    if not isinstance(info, dict):
        return None, []
    if info.get("active"):
        await remote_diag_call(node, target, {
            "cmd": DIAG_CAPTURE_COMMAND,
            "bytes": node.received_bytes,
        })
        info = await remote_diag_call(
            node, target, {"cmd": DIAG_INFO_COMMAND}
        )
        if not isinstance(info, dict):
            return None, []

    records = []
    generation = info.get("g")
    count = info.get("n", 0)
    for index in range(count):
        record = []
        page_count = (
            len(DIAG_META_NAMES) + len(CORE_STAT_NAMES) +
            DIAG_PAGE_VALUES - 1
        ) // DIAG_PAGE_VALUES
        for page in range(page_count):
            reply = await remote_diag_call(node, target, {
                "cmd": DIAG_READ_COMMAND,
                "g": generation,
                "i": index,
                "p": page,
            })
            if not isinstance(reply, dict) or \
                    reply.get("g") != generation or \
                    reply.get("i") != index or reply.get("p") != page or \
                    not isinstance(reply.get("r"), list):
                record = []
                break
            record.extend(reply["r"])
        if record:
            records.append(record)
    return info, records


def print_slave_diagnostics(info, records):
    if info is None:
        print("slave diagnostics unavailable")
        return
    print("slave diag info:", info)
    if not records:
        print("slave diag: no records")
        return
    baseline = records[0]
    baseline_stats = baseline[len(DIAG_META_NAMES):]
    baseline_ticks = baseline[2]
    print("slave diag stat order:", CORE_STAT_NAMES)
    for record in records:
        if len(record) != len(DIAG_META_NAMES) + len(CORE_STAT_NAMES):
            print("slave diag malformed record:", record)
            continue
        meta = dict(zip(DIAG_META_NAMES, record[:len(DIAG_META_NAMES)]))
        meta["tag_name"] = DIAG_TAG_NAMES.get(meta["tag"], "unknown")
        meta["elapsed_ms"] = counter_delta(meta["ticks_ms"], baseline_ticks)
        stats = record[len(DIAG_META_NAMES):]
        delta = [counter_delta(stats[index], baseline_stats[index])
                 for index in range(len(CORE_STAT_NAMES))]
        print("slave diag meta:", meta)
        print("slave diag stats:", stats)
        print("slave diag delta:", delta)


async def find_target(node):
    quantity = await node.get_nodes_qty()
    print("online nodes:", quantity)
    for index in range(quantity):
        info = await node.get_node_info(index)
        print("node", index, info)
        node_id = info.get("id") if info else None
        if node_id is not None and node_id != node.node_id:
            return node_id
    return None


async def wait_for_stream(node, event, timeout):
    event_task = asyncio.create_task(event.wait())
    failure_task = asyncio.create_task(node.stream_failed.wait())
    try:
        done, unused_pending = await asyncio.wait(
            (event_task, failure_task), timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
        if node.failure is not None:
            raise StreamFailure(str(node.failure))
    finally:
        for task in (event_task, failure_task):
            if not task.done():
                task.cancel()


async def main():
    node = await AsyncPCNode.create(port=PORT)
    phase = "setup"
    target = None
    master_before = None
    master_after = None
    try:
        phase = "registration"
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            await asyncio.sleep(1)
        print("PC node ID:", node.node_id)

        phase = "node enumeration"
        target = await find_target(node)
        if target is None:
            print("No remote node available")
            return

        node.test_source = target
        node.expected_bytes = TEST_BYTES
        reset_reply = await remote_diag_call(
            node, target, {"cmd": DIAG_RESET_COMMAND}
        )
        print("slave diag reset:", reset_reply)
        if hasattr(node, "get_core_diagnostics"):
            phase = "master diagnostics"
            master_before = await node.get_core_diagnostics()
        print("requesting", TEST_BYTES, "bytes from node", target)
        phase = "stream command"
        reply = await node.send_command_and_wait_reply(
            target,
            {"cmd": STREAM_COMMAND, "bytes": TEST_BYTES},
            timeout_ms=5000,
        )
        print("command reply:", reply)
        if not reply.get("ok"):
            return

        phase = "pipe open"
        await wait_for_stream(node, node.stream_opened, 5)
        timeout = max(15.0, TEST_BYTES / 4000.0)
        phase = "pipe data"
        await wait_for_stream(node, node.stream_finished, timeout)
        phase = "pipe close"
        await wait_for_stream(node, node.stream_closed, 15)

        elapsed = node.stream_finished_at - node.stream_started_at
        byte_rate = node.received_bytes / elapsed
        bit_rate = byte_rate * 8
        print("received:", node.received_bytes, "bytes")
        print("invalid:", node.invalid_bytes, "bytes")
        print("elapsed: {:.3f} s".format(elapsed))
        print("throughput: {:.1f} B/s ({:.1f} kbit/s)".format(
            byte_rate, bit_rate / 1000.0
        ))
    except asyncio.TimeoutError:
        print("TEST! timeout waiting for", phase, "after",
              node.received_bytes, "bytes")
    except StreamFailure as error:
        print("TEST!", error, "after", node.received_bytes, "bytes")
    except (TransportProxyRemoteError, TransportProxyTimeout) as error:
        print("TEST!", phase, "proxy failure:", error, "after",
              node.received_bytes, "bytes")
    except asyncio.CancelledError:
        pass
    finally:
        if target is not None:
            if hasattr(node, "get_core_diagnostics"):
                try:
                    master_after = await node.get_core_diagnostics()
                except Exception as error:
                    print("master diag request failed:", error)
            info, records = await collect_slave_diagnostics(node, target)
            print_gap_summary(node)
            print_core_diagnostics("master", master_before, master_after)
            print_slave_diagnostics(info, records)
        await node.close()


if __name__ == "__main__":
    try:
        while True:
            asyncio.sleep( 1.0 )
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
