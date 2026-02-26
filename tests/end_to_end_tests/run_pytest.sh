#!/bin/bash
# Note that because of the probabalistic nature of the LLMs, the tests may fail
# which is why pytest is run with the --last-failed option, so that if a test fails,
# it will be run again the next time pytest is run.
# A success is then a run of pytest where all tests either pass or are skipped because 
# they were passed in a previous run.

# Not all python installations support --tui by default, so we do a little test. 
# If `pytest --help` contains the string "tui", then it is supported, otherwise we will run pytest without it.
if pytest --help | grep -q "tui"; then
    pytest --last-failed --tui "$@" # Default, no need to echo anything
else
    echo "pytest-tui is not supported, running pytest without --tui"
    pytest --last-failed "$@"
fi

# Example usage: ./tests/end_to_end_tests/run_pytest.sh --freva-user-token="..."