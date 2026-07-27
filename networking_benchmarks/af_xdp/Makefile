CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -O3 -march=native -mtune=native
CXXFLAGS += -flto -fno-omit-frame-pointer -g
CXXFLAGS += -DNDEBUG -funroll-loops -MMD -MP
CXXFLAGS += -msse4.2 -mavx2 -mfma
INCLUDES = -I./replicator -I/usr/local/include
LDFLAGS = -L/usr/local/lib -Wl,-rpath,/usr/local/lib -Wl,--no-as-needed -Wl,-O1
LDLIBS = -lxdp -lbpf -lelf -lpthread

# ── Replicator object files ──────────────────────────────────────────────────
REPLICATOR_SRCS = replicator/AFXDPSocket.cpp replicator/NetworkInterfaceConfigurator.cpp replicator/PacketReplicator.cpp
REPLICATOR_OBJS = $(REPLICATOR_SRCS:.cpp=.o)

# ── Targets ──────────────────────────────────────────────────────────────────
CORE_TARGETS = packet_replicator probe ctl ping
MCAST_TARGETS = mcast_sender mcast_receiver
XDP_PROGRAMS = xdp/ucast_filter.o
MCAST_XDP = xdp/mcast_filter.o

# ── Build modes ──────────────────────────────────────────────────────────────
all: $(CORE_TARGETS) $(XDP_PROGRAMS)
full: all mcast
mcast: $(MCAST_TARGETS) $(MCAST_XDP)

# ── Pattern rules ────────────────────────────────────────────────────────────
replicator/%.o: replicator/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

tools/%.o: tools/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

cli/%.o: cli/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

legacy/%.o: legacy/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

xdp/%.o: xdp/%.c
	clang -O2 -g -Wall -target bpf -c $< -o $@

# ── Core: replicator ─────────────────────────────────────────────────────────
packet_replicator: replicator/PacketReplicatorMain.o $(REPLICATOR_OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── Tools: measurement instruments ───────────────────────────────────────────
probe: tools/probe.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

mcast_sender: tools/mcast_sender.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

mcast_receiver: tools/mcast_receiver.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── CLI: admin utilities ──────────────────────────────────────────────────────
ctl: cli/ctl.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

ping: cli/ping.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── Legacy (not built by default, use: make mdp_client) ───────────────────────
mdp_client: legacy/mdp_client.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── Housekeeping ─────────────────────────────────────────────────────────────
clean:
	find . -name '*.o' -o -name '*.d' | xargs rm -f
	rm -f $(CORE_TARGETS) $(MCAST_TARGETS) mdp_client

.PHONY: all full mcast clean
