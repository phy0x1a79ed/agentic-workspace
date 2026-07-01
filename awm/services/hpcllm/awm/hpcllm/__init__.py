"""awm.hpcllm — the HPC LLM serving feature service: on-demand LLM server
lifecycles on HPC clusters (Sockeye V100, later Fir H100). Users request a model
via ``serve_request``, receive async status updates via the ``serve.status``
emitter, and get a localhost endpoint once the server is ready. Powered by
LLaMA.cpp for fast startup (no kernel compilation) inside Apptainer containers.
"""
