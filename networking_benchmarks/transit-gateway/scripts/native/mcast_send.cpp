// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// mcast_send - native replacement for scripts/mcast_send.py.
//
// Sends sequenced UDP datagrams to a multicast group, each carrying the sender's
// CLOCK_REALTIME stamp. Wire format is identical to the Python sender, so either
// receiver works with either sender:
//
//   offset size field    encoding
//        0    4 seq      uint32, network byte order
//        4    8 tx_ns    uint64, network byte order, ns since epoch (CLOCK_REALTIME)
//   (optional zero padding up to <payload_bytes>)
//
//   End of stream: seq == 0xFFFFFFFF, tx_ns carries the number of data packets
//   sent. Sent three times, 50 ms apart.
//
// The loop follows af_xdp/tools/mcast_send.cpp (run_kernel_send):
//   - one preallocated buffer, only seq and tx_ns rewritten per packet
//   - tx_ns taken immediately before sendto()
//   - absolute-deadline pacing, t0 + i * (1e9 / rate), so overshoot is absorbed
//     instead of accumulating (the Python sender paced off each send's own stamp)
//   - achieved vs requested rate reported over the active window
// Waiting is a spin on CLOCK_MONOTONIC rather than clock_nanosleep: it keeps the
// send path hot in cache and avoids timer wake-up latency before the stamp.
//
// Usage: mcast_send <group> <port> <rate_pps> <duration_secs> <src_ip> [payload_bytes]
// Output: one JSON line on stdout (summary). Exit 0 on success.

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "lat_stats.hpp"
#ifdef __linux__
#include <sched.h>
#endif

namespace {

constexpr uint32_t kEndSeq = 0xFFFFFFFFu;
constexpr int kMinPayload = 12;
constexpr int kMaxPayload = 1472;                   // fits a 1500-byte MTU
constexpr int64_t kResyncThresholdNs = 100000000;  // 100 ms behind: shift the schedule
constexpr int kFatalConsecutiveErrors = 1000;

volatile sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

inline int64_t mono_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}
inline uint64_t real_ns() {
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL + static_cast<uint64_t>(ts.tv_nsec);
}
inline void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__)
    asm volatile("yield" ::: "memory");
#endif
}
inline void put_be32(uint8_t* p, uint32_t v) {
    p[0] = static_cast<uint8_t>(v >> 24); p[1] = static_cast<uint8_t>(v >> 16);
    p[2] = static_cast<uint8_t>(v >> 8);  p[3] = static_cast<uint8_t>(v);
}
inline void put_be64(uint8_t* p, uint64_t v) {
    put_be32(p, static_cast<uint32_t>(v >> 32));
    put_be32(p + 4, static_cast<uint32_t>(v));
}

bool parse_int(const char* s, long lo, long hi, long* out) {
    char* end = nullptr;
    errno = 0;
    const long v = strtol(s, &end, 10);
    if (errno || end == s || *end != '\0' || v < lo || v > hi) return false;
    *out = v;
    return true;
}

int usage(const char* argv0) {
    fprintf(stderr,
            "usage: %s <group> <port> <rate_pps> <duration_secs> <src_ip> [payload_bytes]\n"
            "  rate_pps 0 sends as fast as possible; payload_bytes %d..%d (default %d)\n",
            argv0, kMinPayload, kMaxPayload, kMinPayload);
    return 2;
}

int fail(const char* msg, const char* detail) {
    printf("{\"tool\":\"mcast_send\",\"impl\":\"cpp\",\"error\":\"%s\",\"detail\":\"%s\"}\n", msg, detail);
    return 1;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 6 || argc > 7) return usage(argv[0]);

    long port = 0, rate = 0, duration = 0, payload = kMinPayload;
    if (!parse_int(argv[2], 1, 65535, &port) || !parse_int(argv[3], 0, 10000000, &rate) ||
        !parse_int(argv[4], 1, 86400, &duration) ||
        (argc == 7 && !parse_int(argv[6], kMinPayload, kMaxPayload, &payload)))
        return usage(argv[0]);

    sockaddr_in dst{};
    dst.sin_family = AF_INET;
    dst.sin_port = htons(static_cast<uint16_t>(port));
    in_addr src{};
    if (inet_pton(AF_INET, argv[1], &dst.sin_addr) != 1) return fail("invalid group", argv[1]);
    if (inet_pton(AF_INET, argv[5], &src) != 1) return fail("invalid src_ip", argv[5]);

#ifdef __linux__
    {   // Same CPU as the Python sender (best effort).
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(0, &set);
        sched_setaffinity(0, sizeof set, &set);
    }
#endif

    const int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) return fail("socket failed", strerror(errno));
    const int ttl = 32;
    setsockopt(sock, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof ttl);
    if (setsockopt(sock, IPPROTO_IP, IP_MULTICAST_IF, &src, sizeof src) < 0)
        return fail("IP_MULTICAST_IF failed", strerror(errno));
    const int sndbuf = 4 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof sndbuf);

    struct sigaction sa{};
    sa.sa_handler = on_signal;           // no SA_RESTART: interrupt blocking calls
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    uint8_t buf[kMaxPayload] = {0};
    const size_t len = static_cast<size_t>(payload);

    const int64_t t0 = mono_ns();
    const int64_t t_end = t0 + duration * 1000000000LL;
    int64_t shift = 0;                   // schedule shift after a long stall
    uint64_t slot = 0, sent = 0, send_errors = 0, late_gt_1ms = 0, resyncs = 0;
    int consecutive_errors = 0;
    int64_t late_max = 0;
    latstats::SignedHist send_cost;   // time spent inside sendto(), per packet
    uint64_t first_tx = 0, last_tx = 0;
    int last_errno = 0;

    while (!g_stop) {
        int64_t now;
        if (rate > 0) {
            const int64_t deadline =
                t0 + shift + static_cast<int64_t>((static_cast<__int128>(slot) * 1000000000LL) / rate);
            if (deadline >= t_end) break;
            while ((now = mono_ns()) < deadline) {
                cpu_relax();
                if (g_stop) break;
            }
            if (g_stop) break;
            const int64_t late = now - deadline;
            if (late > late_max) late_max = late;
            if (late > 1000000) ++late_gt_1ms;
            if (late > kResyncThresholdNs) {  // do not release a backlog as a burst
                shift += late;
                ++resyncs;
            }
        } else {
            now = mono_ns();
            if (now >= t_end) break;
        }
        ++slot;

        put_be32(buf, static_cast<uint32_t>(sent));
        const uint64_t tx = real_ns();       // immediately before the send
        put_be64(buf + 4, tx);
        const int64_t m_before = mono_ns();
        const ssize_t n = sendto(sock, buf, len, 0, reinterpret_cast<sockaddr*>(&dst), sizeof dst);
        send_cost.add(mono_ns() - m_before);
        if (n == static_cast<ssize_t>(len)) {
            if (sent == 0) first_tx = tx;
            last_tx = tx;
            ++sent;                          // sequence numbers stay gap-free
            consecutive_errors = 0;
        } else {
            ++send_errors;
            last_errno = errno;
            if (++consecutive_errors >= kFatalConsecutiveErrors) {
                fprintf(stderr, "mcast_send: aborting after %d consecutive send errors (%s)\n",
                        consecutive_errors, strerror(last_errno));
                break;
            }
        }
    }
    const bool interrupted = g_stop != 0;
    const int64_t t_stop = mono_ns();

    // End-of-stream marker, three times, carrying the data-packet count.
    {
        uint8_t endbuf[kMinPayload];
        put_be32(endbuf, kEndSeq);
        put_be64(endbuf + 4, sent);
        for (int i = 0; i < 3; ++i) {
            sendto(sock, endbuf, sizeof endbuf, 0, reinterpret_cast<sockaddr*>(&dst), sizeof dst);
            const timespec ts{0, 50 * 1000 * 1000};
            nanosleep(&ts, nullptr);
        }
    }
    close(sock);

    rusage ru{};
    getrusage(RUSAGE_SELF, &ru);
    const double window_s = sent > 1 ? static_cast<double>(last_tx - first_tx) / 1e9 : 0.0;
    const double achieved = window_s > 0 ? static_cast<double>(sent - 1) / window_s : 0.0;
    printf("{\"tool\":\"mcast_send\",\"impl\":\"cpp\",\"group\":\"%s\",\"port\":%ld,\"src_ip\":\"%s\","
           "\"payload_bytes\":%ld,\"requested_pps\":%ld,\"duration_s\":%ld,\"sent\":%llu,"
           "\"send_errors\":%llu,\"elapsed_s\":%.3f,\"active_window_s\":%.3f,\"achieved_pps\":%.1f,"
           "\"achieved_pct\":%.2f,\"late_max_us\":%.1f,\"late_gt_1ms_count\":%llu,\"resyncs\":%llu,"
           "\"sendto_median_us\":%.3f,\"sendto_p99_us\":%.3f,\"sendto_max_us\":%.3f,"
           "\"cpu_user_s\":%.3f,\"cpu_sys_s\":%.3f,\"interrupted\":%s}\n",
           argv[1], port, argv[5], payload, rate, duration, static_cast<unsigned long long>(sent),
           static_cast<unsigned long long>(send_errors), static_cast<double>(t_stop - t0) / 1e9, window_s,
           achieved, rate > 0 ? 100.0 * achieved / static_cast<double>(rate) : 0.0,
           static_cast<double>(late_max) / 1000.0, static_cast<unsigned long long>(late_gt_1ms),
           static_cast<unsigned long long>(resyncs),
           send_cost.count() ? static_cast<double>(send_cost.quantile(0.5)) / 1000.0 : 0.0,
           send_cost.count() ? static_cast<double>(send_cost.quantile(0.99)) / 1000.0 : 0.0,
           send_cost.count() ? static_cast<double>(send_cost.max()) / 1000.0 : 0.0,
           ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6,
           ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6, interrupted ? "true" : "false");
    return consecutive_errors >= kFatalConsecutiveErrors ? 1 : 0;
}
