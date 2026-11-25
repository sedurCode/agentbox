# syntax=docker/dockerfile:1
# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# The v2 kit's sandbox.image and sandbox.entrypoint; Junie itself is installed by the
# kit's install hooks (see junie.yaml).
FROM docker/sandbox-templates:shell-docker
ENTRYPOINT ["junie"]
