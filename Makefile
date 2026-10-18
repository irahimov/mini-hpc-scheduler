PY ?= python3

.PHONY: test test-fast check experiments experiments-quick demo clean

test:            ## all tests (~1 min, starts real daemons and real processes)
	$(PY) -W ignore -m unittest discover -s tests -t .

test-fast:       ## pure logic + parsing tests only (~1 s)
	$(PY) -W ignore -m unittest tests.test_planner tests.test_units

check:           ## environment check (WSL2 / Linux)
	bash scripts/setup_wsl2.sh

experiments:     ## full experiment run -> experiments/results/ (~4 min)
	$(PY) -W ignore experiments/run_experiments.py

experiments-quick:
	$(PY) -W ignore experiments/run_experiments.py --quick

demo:            ## everything scripted for the live defense (DEMO_SCALE=1 for the real 60s/5s test)
	bash scripts/demo.sh all

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
