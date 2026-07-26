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
CORE_TARGETS = packet_replicator control_client test_client market_data_provider_client latency_client
MCAST_TARGETS = mcast_sender mcast_receiver
XDP_PROGRAMS = xdp/unicast_filter.o
MCAST_XDP = xdp/gre_filter.o

# ── Build modes ──────────────────────────────────────────────────────────────
all: $(CORE_TARGETS) $(XDP_PROGRAMS)
full: all mcast
mcast: $(MCAST_TARGETS) $(MCAST_XDP)

# ── Pattern rules ────────────────────────────────────────────────────────────
replicator/%.o: replicator/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

clients/%.o: clients/%.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

xdp/%.o: xdp/%.c
	clang -O2 -g -Wall -target bpf -c $< -o $@

# ── Core binaries ────────────────────────────────────────────────────────────
packet_replicator: replicator/PacketReplicatorMain.o $(REPLICATOR_OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

control_client: clients/ControlClient.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

test_client: clients/TestClient.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

market_data_provider_client: clients/MarketDataProviderClient.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

latency_client: clients/latency_client.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── Mcast one-way measurement tools ─────────────────────────────────────────
mcast_sender: clients/mcast_sender.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

mcast_receiver: clients/mcast_receiver.o
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

# ── Housekeeping ─────────────────────────────────────────────────────────────
clean:
	find . -name '*.o' -o -name '*.d' | xargs rm -f
	rm -f $(CORE_TARGETS) $(MCAST_TARGETS)

help:
	@echo "Targets:"
	@echo "  all    - Build core binaries + unicast XDP (default)"
	@echo "  mcast  - Build multicast one-way tools + GRE XDP"
	@echo "  full   - Build everything (all + mcast)"
	@echo "  clean  - Remove all build artifacts"
	@echo ""
	@echo "Core:  $(CORE_TARGETS)"
	@echo "Mcast: $(MCAST_TARGETS)"
	@echo "XDP:   $(XDP_PROGRAMS) $(MCAST_XDP)"

.PHONY: all full mcast clean help

-include $(wildcard replicator/*.d clients/*.d)
