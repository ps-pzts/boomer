#!/usr/bin/env bash
# Mutation scores for the money-critical modules (#89). Slow (tens of minutes): run by hand,
# never in CI.  usage: scripts/mutation_all.sh [outdir]
set -u
out="${1:-reports/mutation}"
mkdir -p "$out"
run() { # module tests... [--func f]
  name=$(basename "$1" .py)${FUNC:+_$FUNC}
  python scripts/mutation.py "$@" --json "$out/$name.json" > "$out/$name.log" 2>&1
  tail -1 "$out/$name.log" | head -0
  grep "killed" "$out/$name.log" | tail -1
}
run src/executor/trailing.py tests/executor/test_trailing.py
run src/executor/ticks.py tests/executor/test_ticks.py
run src/capital/sizing.py tests/capital
run src/capital/pre_trade.py tests/capital
run src/capital/circuit_breakers.py tests/capital
run src/executor/trade_machine.py tests/executor/test_trade_machine.py tests/executor/test_trade_lifecycle.py
run src/executor/session_machine.py tests/executor/test_session_machine.py tests/executor/test_session_controller.py tests/executor/test_session_kite_gate.py
run src/executor/session_inputs.py tests/executor/test_session_inputs.py tests/executor/test_untracked_orders.py tests/executor/test_session_controller.py
FUNC=map_kite_status run src/executor/brokers/kite_broker.py tests/executor/brokers/test_kite_order_status.py tests/executor/brokers/test_kite_broker.py --func map_kite_status
run src/executor/brokers/kitesim/rules.py tests/executor/brokers/kitesim
run src/executor/trade_lifecycle.py tests/executor/test_trade_lifecycle.py tests/executor/test_lost_orders.py tests/integration/test_trade_paths.py tests/integration/test_scenarios.py
