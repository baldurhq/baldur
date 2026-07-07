# ClusterFuzzLite build image for the Baldur fuzz harnesses.
# The base image ships Atheris and the OSS-Fuzz Python fuzzing toolchain.
FROM gcr.io/oss-fuzz-base/base-builder-python

# Copy the whole repository into the OSS-Fuzz source tree.
COPY . $SRC/baldur
WORKDIR $SRC/baldur

# `compile` runs $SRC/build.sh, which builds every harness under fuzz/.
COPY .clusterfuzzlite/build.sh $SRC/build.sh
