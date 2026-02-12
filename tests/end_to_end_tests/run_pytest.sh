#!/bin/bash
# Note that because of the probabalistic nature of the LLMs, the tests may fail
# which is why pytest is run with the --last-failed option, so that if a test fails,
# it will be run again the next time pytest is run.
# A success is then a run of pytest where all tests either pass or are skipped because 
# they were passed in a previous run.
pytest --last-failed --tui "$@" # Note: this requires pytest-tui to be installed.

# Example usage: ./tests/end_to_end_tests/run_pytest.sh --freva-user-token="..."