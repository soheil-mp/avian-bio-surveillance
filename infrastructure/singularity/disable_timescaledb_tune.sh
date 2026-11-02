#!/bin/sh
# Local WSL / Apptainer compatibility shim.
#
# When bind-mounted over the image's timescaledb-tune init script, this keeps
# first boot working in environments where timescaledb-tune crashes during
# cgroup or memory detection.
exit 0