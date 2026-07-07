#!/bin/bash -eu
# ClusterFuzzLite build script — compiles the Atheris harnesses under fuzz/.

# Install the framework (pulls orjson and the runtime deps) so the harnesses
# can import baldur. The harnesses live at repo-root fuzz/ and are therefore
# never packaged into the distributed wheel.
pip3 install "$SRC/baldur"

# Compile each harness into a standalone libFuzzer binary in $OUT.
for harness in "$SRC"/baldur/fuzz/fuzz_*.py; do
  compile_python_fuzzer "$harness"
done
