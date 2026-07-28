# src/

Core replicator engine — AF_XDP zero-copy packet processing and kernel-mode echo.

## Files

| File | Description |
|------|-------------|
| `ReplicatorMain.cpp` | Entry point — CLI parsing, mode dispatch (`--kernel-mode` or AF_XDP) |
| `Replicator.cpp/hpp` | AF_XDP packet replicator: multi-queue RX, destination fan-out, control protocol |
| `XdpSocket.cpp/hpp` | AF_XDP socket wrapper: UMEM, ring buffer management, zero-copy TX/RX |
| `KernelEcho.cpp` | Kernel-mode UDP echo server (same control protocol, no root/XDP needed) |
| `NicConfig.cpp/hpp` | NIC configuration helpers (queue count, coalescing, MTU) |
| `xdp/ucast.c` | eBPF XDP program — unicast filter (steers matching packets to AF_XDP socket) |
| `xdp/mcast.c` | eBPF XDP program — multicast/GRE filter (decaps GRE, steers inner multicast) |

## Architecture

```
ReplicatorMain
  ├── --kernel-mode → KernelEcho (UDP socket echo, port 12345 control)
  └── AF_XDP mode  → Replicator
                       ├── XdpSocket (per-queue AF_XDP rings)
                       ├── NicConfig (interface setup)
                       └── xdp/ucast.o or xdp/mcast.o (BPF program)
```

## Control Protocol (port 12345)

Binary protocol, same in both modes:

| Opcode | Payload | Action |
|--------|---------|--------|
| `0x01` | 4B IP + 2B port | ADD destination |
| `0x02` | 4B IP + 2B port | REMOVE destination |
| `0x03` | (none) | LIST destinations |

## Build modes

```bash
make all          # Full AF_XDP build (requires libxdp, libbpf)
make kernel-mode  # No libxdp dep (containers, CI, any Linux)
make full         # all + multicast targets
```

The `#ifdef KERNEL_MODE_ONLY` guards in `ReplicatorMain.cpp` exclude XDP code paths when building with `-DKERNEL_MODE_ONLY`.
