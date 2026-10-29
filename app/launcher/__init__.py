"""Dedicated review-container launcher (holds the Docker socket).

M2: the only process that talks to the Docker API. Workers call its authenticated
HTTP API with ``{job_id, checkout_ref}`` only; container create specs are hard-coded
here (image, mounts, caps, limits, network, labels). See SECURITY.md.
"""
